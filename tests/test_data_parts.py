import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.restore_compressed_sources import restore


class DataPartsTests(unittest.TestCase):
    def test_pack_keeps_source_and_roundtrips_bounded_parts(self):
        from scripts.data_parts import pack_file
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'data/candidates.json'
            source.parent.mkdir()
            body = bytes(range(256)) * 10
            source.write_bytes(body)
            manifest_path = pack_file(source, part_size=31)
            self.assertEqual(source.read_bytes(), body)
            manifest = json.loads(manifest_path.read_text())
            self.assertEqual(manifest['sha256'], hashlib.sha256(body).hexdigest())
            self.assertGreater(len(manifest['parts']), 1)
            self.assertTrue(all(0 < p['size'] <= 31 for p in manifest['parts']))
            source.unlink()
            self.assertEqual(restore(root), 1)
            self.assertEqual(source.read_bytes(), body)

    def test_pack_uses_sqlite_backup_to_include_wal(self):
        import sqlite3
        from scripts.data_parts import pack_file
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'data/names.db'
            path.parent.mkdir()
            connection = sqlite3.connect(path)
            self.addCleanup(connection.close)
            connection.execute('pragma journal_mode=wal')
            connection.execute('create table sample(value text)')
            connection.execute("insert into sample values ('WAL row')")
            connection.commit()
            manifest = pack_file(path, part_size=100)
            other = root / 'restored/data/names.db.gz.parts'
            import shutil
            shutil.copytree(manifest.parent, other)
            restore(root / 'restored')
            with sqlite3.connect(other.parent / 'names.db') as restored:
                self.assertEqual(restored.execute('select value from sample').fetchall(), [('WAL row',)])
