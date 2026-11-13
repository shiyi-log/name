#!/usr/bin/env python3
"""检查 Git 跟踪文件和 SQLite 字段，阻止常见密钥进入发布内容。"""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "OpenAI API 密钥": re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}"),
    "GitHub 令牌": re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    "AWS 访问密钥": re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "私钥": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----"),
}


@dataclass(frozen=True)
class Finding:
    path: str
    location: str
    kind: str
    count: int = 1


def scan_bytes(path: str, location: str, body: bytes) -> list[Finding]:
    """仅保留匹配种类和数量，不保留密钥原文。"""
    return [
        Finding(path, location, kind, count)
        for kind, pattern in PATTERNS.items()
        if (count := sum(1 for _ in pattern.finditer(body)))
    ]


def scan_file(path: Path, relative: str, location: str = "文件内容") -> list[Finding]:
    """Scan large files in chunks without loading them all into memory."""
    findings = []
    tail = b""
    counts = Counter()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            body = tail + chunk
            for kind, pattern in PATTERNS.items():
                matches = list(pattern.finditer(body))
                counts[kind] += sum(match.start() + len(match.group()) > len(tail) for match in matches)
            tail = body[-128:]
    findings.extend(Finding(relative, location, kind, count) for kind, count in counts.items() if count)
    return findings


def forbidden_path(path: Path) -> bool:
    name = path.name.lower()
    return (
        (name == ".env" or name.startswith(".env.")) and name != ".env.example"
        or name.endswith((".pem", ".key"))
        or name.startswith("cc-switch.db")
        or ".cc-switch" in path.parts
    )


def scan_database(path: Path, relative: str) -> list[Finding]:
    """只读检查数据库的文本字段，结果不包含字段内容。"""
    findings = []
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        for (table,) in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            cursor = connection.execute(f"SELECT * FROM {quoted}")
            columns = [column[0] for column in cursor.description]
            counts: Counter[tuple[str, str]] = Counter()
            for row in cursor:
                for column, value in zip(columns, row):
                    if isinstance(value, str):
                        for finding in scan_bytes(relative, column, value.encode("utf-8")):
                            counts[(column, finding.kind)] += finding.count
            findings.extend(
                Finding(relative, f"表 {table} / 列 {column}", kind, count)
                for (column, kind), count in sorted(counts.items())
            )
    return findings


def check_repository(root: Path) -> tuple[int, list[Finding]]:
    output = subprocess.check_output(["git", "-C", str(root), "ls-files", "-z"])
    files = sorted(set(name for name in output.decode("utf-8").split("\0") if name))
    changed = set(subprocess.check_output([
        "git", "-C", str(root), "diff", "--name-only", "-z",
    ]).decode("utf-8").split("\0"))
    findings = []
    for relative in files:
        path = root / relative
        if forbidden_path(Path(relative)):
            findings.append(Finding(relative, "文件路径", "禁止提交的密钥配置文件"))
            continue
        if path.is_symlink():
            findings.append(Finding(relative, "文件路径", "需要检查的符号链接"))
            continue
        try:
            if path.exists():
                # 同时扫描数据库原始字节，避免已删除记录的残留密钥随文件上传。
                findings.extend(scan_file(path, relative))
                if path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
                    findings.extend(scan_database(path, relative))
            if relative in changed:
                # 部分暂存时，索引才是将被提交的版本，不能仅检查工作区。
                if path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
                    with tempfile.TemporaryDirectory(prefix="name-secret-index-") as directory:
                        snapshot = Path(directory) / "staged.db"
                        with snapshot.open("wb") as output:
                            subprocess.run(["git", "-C", str(root), "show", ":" + relative], stdout=output, check=True)
                        findings.extend(scan_file(snapshot, relative, "暂存内容"))
                        findings.extend(scan_database(snapshot, relative))
                else:
                    staged = subprocess.check_output(["git", "-C", str(root), "show", ":" + relative])
                    findings.extend(scan_bytes(relative, "暂存内容", staged))
        except (OSError, sqlite3.Error, subprocess.CalledProcessError):
            findings.append(Finding(relative, "文件读取", "无法完成密钥检查"))
    return len(files), findings


def main() -> None:
    parser = argparse.ArgumentParser(description="检查 Git 跟踪文件中的常见 API 密钥和私钥")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    count, findings = check_repository(args.root.resolve())
    if findings:
        print(f"密钥检查失败：检查 {count} 个文件，发现 {len(findings)} 项风险。")
        for finding in findings:
            print(f"{finding.path} | {finding.location} | {finding.kind} | {finding.count} 处")
        raise SystemExit(1)
    print(f"密钥检查通过：已检查 {count} 个 Git 跟踪文件及 SQLite 文本字段。")


if __name__ == "__main__":
    main()
