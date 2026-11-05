from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from scripts import package_release


class ReleasePackageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "source"
        self.root.mkdir()
        self.output = Path(self.directory.name) / "dist"
        self.root_patch = patch.object(package_release, "ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.git("init", "-q")
        for filename in (
            "api_server.py", "app.js", "index.html", "styles.css", "build_database.py",
            "scoring.py", "requirements.txt", "README.md", "THIRD_PARTY_NOTICES.md",
            "restored_candidates_all_history.txt", "已排除名字.md",
            "data/characters-8105.json", "data/tokens.json", "data/generation_state.json",
        ):
            destination = self.root / filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text("fixture\n", encoding="utf-8")
        (self.root / "VERSION").write_text("0.1.0\n")
        (self.root / "CHANGELOG.md").write_text("# 版本记录\n\n## 0.1.0 — 2026-11-05\n\n发布验证。\n")
        (self.root / "data/candidates.json").write_text(json.dumps([
            {"name": "云山", "id": "candidate-1", "score": 80.5},
        ]))
        with closing(sqlite3.connect(self.root / "data/names.db")) as connection:
            connection.execute("CREATE TABLE characters (id INTEGER PRIMARY KEY)")
            connection.executemany("INSERT INTO characters VALUES (?)", [(index,) for index in range(8105)])
            connection.execute("CREATE TABLE name_candidates (name TEXT, id TEXT, total_score REAL)")
            connection.execute("INSERT INTO name_candidates VALUES ('云山', 'candidate-1', 80.5)")
            connection.commit()
        self.commit()

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.root), *args], stderr=subprocess.STDOUT)

    def commit(self):
        self.git("add", ".")
        self.git("-c", "user.name=发布测试", "-c", "user.email=release-test@example.invalid", "commit", "-qm", "测试提交")

    def test_archive_uses_committed_files_and_records_checksums(self):
        (self.root / "api_server.py").write_text("dirty workspace\n")
        (self.root / "untracked-secret.txt").write_text("test-only-token\n")
        artifact = package_release.package_release("v0.1.0", "HEAD", self.output)
        with tarfile.open(artifact) as archive:
            self.assertEqual(archive.extractfile("name-v0.1.0/api_server.py").read(), b"fixture\n")
            self.assertNotIn("name-v0.1.0/untracked-secret.txt", archive.getnames())
        manifest = json.loads((self.output / "release-manifest.json").read_text())
        self.assertEqual(manifest["candidateCount"], 1)
        self.assertEqual(manifest["characterCount"], 8105)
        self.assertEqual((self.output / "SHA256SUMS").read_text(), f"{manifest['sha256']}  {artifact.name}\n")
        self.assertIn("发布验证", (self.output / "RELEASE_NOTES.md").read_text())

    def test_version_tag_mismatch_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "VERSION"):
            package_release.package_release("v0.2.0", "HEAD", self.output)
        self.assertFalse(self.output.exists())

    def test_inconsistent_database_and_snapshot_are_rejected(self):
        (self.root / "data/candidates.json").write_text(json.dumps([
            {"name": "云山", "id": "candidate-1", "score": 60.0},
        ]))
        self.commit()
        with self.assertRaisesRegex(ValueError, "快照不一致"):
            package_release.package_release("v0.1.0", "HEAD", self.output)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
