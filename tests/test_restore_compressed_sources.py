import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.restore_compressed_sources import restore


class RestoreCompressedSourcesTests(unittest.TestCase):
    def make_parts(self, root, body=b'snapshot'):
        directory = root / 'data/names.db.gz.parts'
        directory.mkdir(parents=True)
        compressed = gzip.compress(body, mtime=0)
        parts = []
        for index, start in enumerate(range(0, len(compressed), 7)):
            chunk = compressed[start:start + 7]
            name = f'part-{index:05d}'
            (directory / name).write_bytes(chunk)
            parts.append({'name': name, 'size': len(chunk), 'sha256': hashlib.sha256(chunk).hexdigest()})
        manifest = {'format': 'gzip-parts-v1', 'size': len(body),
                    'sha256': hashlib.sha256(body).hexdigest(), 'parts': parts}
        (directory / 'manifest.json').write_text(json.dumps(manifest))
        return directory

    def test_split_snapshot_restores_exact_bytes_and_preserves_local_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_parts(root, b'Chinese snapshot: \xe4\xba\x91')
            self.assertEqual(restore(root), 1)
            destination = root / 'data/names.db'
            self.assertEqual(destination.read_bytes(), b'Chinese snapshot: \xe4\xba\x91')
            destination.write_bytes(b'local edits')
            self.assertEqual(restore(root), 0)
            self.assertEqual(destination.read_bytes(), b'local edits')

    def test_corrupt_or_missing_part_never_publishes_partial_snapshot(self):
        for corrupt in (True, False):
            with self.subTest(corrupt=corrupt), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                parts = self.make_parts(root)
                part = parts / 'part-00001'
                if corrupt:
                    part.write_bytes(b'bad data')
                else:
                    part.unlink()
                with self.assertRaises((ValueError, OSError)):
                    restore(root)
                self.assertFalse((root / 'data/names.db').exists())
                self.assertEqual(sorted(p.name for p in (root / 'data').iterdir()), ['names.db.gz.parts'])

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
