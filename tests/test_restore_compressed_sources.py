import gzip
from pathlib import Path
import tempfile
import unittest

from scripts.restore_compressed_sources import restore


class RestoreCompressedSourcesTests(unittest.TestCase):
    def test_restore_writes_missing_files_and_preserves_existing_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            data.mkdir()
            with gzip.open(data / "names.db.gz", "wb") as compressed:
                compressed.write(b"snapshot")
            (data / "candidates.json").write_bytes(b"local edits")
            with gzip.open(data / "candidates.json.gz", "wb") as compressed:
                compressed.write(b"committed snapshot")

            self.assertEqual(restore(root), 1)
            self.assertEqual((data / "names.db").read_bytes(), b"snapshot")
            self.assertEqual((data / "candidates.json").read_bytes(), b"local edits")


if __name__ == "__main__":
    unittest.main()
