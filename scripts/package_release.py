#!/usr/bin/env python3
"""仅使用标准库，从固定的 Git 提交构建带版本号的发布包。"""

from __future__ import annotations

import argparse
from contextlib import closing
import gzip
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile

if __package__:
    from .data_parts import iter_uncompressed
else:
    from data_parts import iter_uncompressed


ROOT = Path(__file__).resolve().parents[1]


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(ROOT), *args])


def release_notes(changelog: str, version: str) -> str:
    sections = re.split(r"(?m)^## ", changelog)
    for section in sections[1:]:
        if section.splitlines()[0].split()[0] == version:
            return "## " + section.rstrip() + "\n"
    raise ValueError(f"CHANGELOG.md 缺少 {version} 的发布说明")


def package_release(tag: str, revision: str, output_dir: Path) -> Path:
    if not re.fullmatch(r"v\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", tag):
        raise ValueError("标签必须采用 v主版本.次版本.修订版本 格式")
    commit = git("rev-parse", "--verify", f"{revision}^{{commit}}").decode().strip()
    version = git("show", f"{commit}:VERSION").decode().strip()
    if tag != f"v{version}":
        raise ValueError(f"标签 {tag} 与提交中的 VERSION={version} 不一致")
    prefix = f"name-{tag}/"
    with tempfile.TemporaryDirectory(prefix="name-package-") as directory:
        scratch = Path(directory)
        archive_path = scratch / "release.tar"
        with archive_path.open("wb") as output:
            subprocess.run(
                ["git", "-C", str(ROOT), "archive", "--format=tar", f"--prefix={prefix}", commit],
                stdout=output, check=True,
            )
        with tarfile.open(archive_path) as archive:
            for member in archive.getmembers():
                if (member.name != prefix.rstrip("/") and not member.name.startswith(prefix)) or ".." in PurePosixPath(member.name).parts:
                    raise ValueError("发布包包含无效路径")
                if member.issym() or member.islnk():
                    raise ValueError("发布包不支持符号链接")

            def read_file(relative_path: str) -> bytes:
                member = archive.extractfile(prefix + relative_path) if prefix + relative_path in archive.getnames() else None
                if member is not None:
                    return member.read()
                parts_prefix = prefix + relative_path + '.gz.parts/'
                if parts_prefix + 'manifest.json' in archive.getnames():
                    manifest = json.load(archive.extractfile(parts_prefix + 'manifest.json'))
                    def read_part(name):
                        if parts_prefix + name not in archive.getnames():
                            raise ValueError(f'发布包缺少分片：{name}')
                        return archive.extractfile(parts_prefix + name).read()
                    return b''.join(iter_uncompressed(manifest, read_part))
                compressed_name = prefix + relative_path + ".gz"
                compressed = archive.extractfile(compressed_name) if compressed_name in archive.getnames() else None
                if compressed is None:
                    raise ValueError(f"发布包缺少文件：{relative_path}")
                with gzip.GzipFile(fileobj=compressed) as source:
                    return source.read()

            for required in (
                "api_server.py", "app.js", "index.html", "styles.css", "build_database.py",
                "scoring.py", "requirements.txt", "README.md", "THIRD_PARTY_NOTICES.md",
                "data/characters-8105.json", "data/tokens.json", "data/generation_state.json",
                "restored_candidates_all_history.txt", "已排除名字.md",
            ):
                read_file(required)
            candidates = json.loads(read_file("data/candidates.json"))
            if not isinstance(candidates, list) or not candidates:
                raise ValueError("候选快照必须是非空列表")
            json_candidates = {item["name"]: (item["id"], item["score"]) for item in candidates}
            if len(json_candidates) != len(candidates):
                raise ValueError("候选快照包含重名")
            database_path = scratch / "names.db"
            database_path.write_bytes(read_file("data/names.db"))
            with closing(sqlite3.connect(database_path.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
                if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("SQLite 完整性检查失败")
                database_candidates = {
                    name: (identifier, score)
                    for name, identifier, score in connection.execute("SELECT name, id, total_score FROM name_candidates")
                }
                if database_candidates != json_candidates:
                    raise ValueError("SQLite 与 JSON 候选快照不一致")
                character_count = connection.execute("SELECT count(*) FROM characters").fetchone()[0]
                if character_count != 8105:
                    raise ValueError(f"完整字库数量异常：{character_count}")
            notes = release_notes(read_file("CHANGELOG.md").decode(), version)
            normalized_path = scratch / "normalized.tar"
            member_names = set(archive.getnames())
            split_paths = {name[len(prefix):-len('.gz.parts/manifest.json')]
                           for name in member_names if name.endswith('.gz.parts/manifest.json')}
            with tarfile.open(normalized_path, "w") as normalized:
                for member in archive.getmembers():
                    if member.name.endswith((".gz", '.gz.parts')) or '.gz.parts/' in member.name:
                        continue
                    source = archive.extractfile(member) if member.isfile() else None
                    if source is None:
                        normalized.addfile(member)
                    else:
                        normalized.addfile(member, source)
                compressed_paths = {name[len(prefix):-3] for name in member_names if name.endswith('.gz')}
                for relative_path in sorted(compressed_paths | split_paths):
                    if prefix + relative_path in member_names:
                        continue
                    body = read_file(relative_path)
                    info = tarfile.TarInfo(prefix + relative_path)
                    info.size = len(body)
                    info.mode = 0o644
                    normalized.addfile(info, io.BytesIO(body))
            archive_path = normalized_path
        output_dir.mkdir(parents=True, exist_ok=True)
        artifact = output_dir / f"name-{tag}.tar.gz"
        with archive_path.open("rb") as source, artifact.open("wb") as destination:
            with gzip.GzipFile(filename="", mode="wb", fileobj=destination, mtime=0) as compressor:
                shutil.copyfileobj(source, compressor)
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        (output_dir / "SHA256SUMS").write_text(f"{digest}  {artifact.name}\n", encoding="utf-8")
        (output_dir / "RELEASE_NOTES.md").write_text(notes, encoding="utf-8")
        manifest = {
            "version": version, "tag": tag, "commit": commit,
            "candidateCount": len(candidates), "characterCount": character_count,
            "artifact": artifact.name, "sha256": digest,
        }
        (output_dir / "release-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return artifact


def main() -> None:
    parser = argparse.ArgumentParser(description="校验版本、数据并构建 Git 提交的发布包")
    parser.add_argument("--tag", required=True)
    parser.add_argument("--revision", default="HEAD", help="需要打包的 Git 提交，默认 HEAD")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    try:
        package_release(args.tag, args.revision, args.output_dir.resolve())
    except (ValueError, KeyError, sqlite3.Error, subprocess.CalledProcessError) as error:
        parser.exit(1, f"发布打包失败：{error}\n")


if __name__ == "__main__":
    main()
