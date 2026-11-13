from contextlib import closing, redirect_stdout
import io
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import check_secrets


class SecretCheckTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)

    def track(self, name, body):
        path = self.root / name
        path.write_text(body)
        subprocess.run(["git", "-C", str(self.root), "add", name], check=True)
        return path

    def test_source_secret_is_blocked_without_disclosing_value(self):
        secret = "sk-" + "a" * 28
        self.track("settings.py", f"API_KEY = {secret!r}\n")
        output = io.StringIO()
        with patch("sys.argv", ["check_secrets.py", "--root", str(self.root)]), redirect_stdout(output):
            with self.assertRaises(SystemExit) as raised:
                check_secrets.main()
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("settings.py", output.getvalue())
        self.assertNotIn(secret, output.getvalue())

    def test_sqlite_secret_reports_column_without_disclosing_value(self):
        secret = "ghp_" + "b" * 30
        database = self.root / "names.db"
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("CREATE TABLE candidates (name TEXT, note TEXT)")
            connection.execute("INSERT INTO candidates VALUES (?, ?)", ("云山", secret))
            connection.commit()
        subprocess.run(["git", "-C", str(self.root), "add", "names.db"], check=True)
        _, findings = check_secrets.check_repository(self.root)
        self.assertTrue(any("列 note" in item.location for item in findings))
        self.assertNotIn(secret, repr(findings))

    def test_staged_secret_cannot_be_hidden_by_safe_worktree_changes(self):
        secret = "sk-" + "c" * 28
        path = self.track("settings.py", secret)
        path.write_text("safe workspace\n")
        _, findings = check_secrets.check_repository(self.root)
        self.assertTrue(any(item.location == "暂存内容" for item in findings))
        self.assertNotIn(secret, repr(findings))

    def test_sensitive_paths_are_blocked_and_example_is_allowed(self):
        self.track(".env", "OPENAI_API_KEY=\n")
        self.track(".env.example", "OPENAI_API_KEY=\n")
        self.track("cc-switch.db", "placeholder")
        _, findings = check_secrets.check_repository(self.root)
        self.assertEqual({item.path for item in findings}, {".env", "cc-switch.db"})

    def test_safe_names_database_and_short_test_token_are_allowed(self):
        self.track("settings.py", 'OPENAI_API_KEY = "test-token"\n')
        database = self.root / "names.db"
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("CREATE TABLE candidates (name TEXT)")
            connection.execute("INSERT INTO candidates VALUES ('云山')")
            connection.commit()
        subprocess.run(["git", "-C", str(self.root), "add", "names.db"], check=True)
        count, findings = check_secrets.check_repository(self.root)
        self.assertEqual(count, 2)
        self.assertEqual(findings, [])

    def test_chunked_scan_finds_secret_split_across_chunk_boundary(self):
        secret = b"sk-" + b"z" * 24
        path = self.root / "large.bin"
        path.write_bytes(b"x" * (1024 * 1024 - 8) + b" " + secret + b"y")
        findings = check_secrets.scan_file(path, "large.bin")
        self.assertEqual([(item.kind, item.count) for item in findings], [("OpenAI API 密钥", 1)])


if __name__ == "__main__":
    unittest.main()
