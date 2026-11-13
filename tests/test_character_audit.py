import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CharacterAuditTests(unittest.TestCase):
    def test_audit_excludes_rejected_uncertain_and_legacy_from_quality_progress(self):
        # Including excluded rows, dropping zero-count characters, accepting a
        # model keep as final approval, or mutating the source DB must fail.
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            database = directory / 'names.db'
            characters = directory / 'characters.json'
            output = directory / 'audit.json'
            characters.write_text(json.dumps(list('云山水月海')))
            with sqlite3.connect(database) as connection:
                connection.execute('CREATE TABLE name_candidates (name TEXT, name_length INTEGER, source_type TEXT, registry_note TEXT, notes_json TEXT, status TEXT)')
                for name, source, status in [
                    ('云山', 'ai', '待核验'), ('山云', 'ai', '待核验'),
                    ('云水', 'ai', '质量复核未通过'),
                    ('云月', 'ai', '质量复核待定'), ('山水', 'rule', '待核验'),
                ]:
                    roles = {ch: '参与名称的自然物象' for ch in name}
                    notes = json.dumps(['质量初筛：各字语义角色=' + json.dumps(roles) + '；自然度自评=5/5'], ensure_ascii=False)
                    connection.execute('INSERT INTO name_candidates VALUES (?,2,?,?,?,?)', (name, source, '', notes, status))
                connection.execute('CREATE TABLE candidate_quality_reviews (name TEXT, review_id TEXT, model TEXT, decision TEXT, reason TEXT)')
                connection.executemany('INSERT INTO candidate_quality_reviews VALUES (?,?,?,?,?)', [
                    ('云山', 'a', 'model', 'reject', '旧判断'),
                    ('云山', 'b', 'model', 'keep', '最新暂留'),
                    ('云水', 'a', 'model', 'reject', '排除'),
                    ('云月', 'a', 'model', 'uncertain', '待定'),
                ])
            before = hashlib.sha256(database.read_bytes()).hexdigest()
            output.write_text(json.dumps({'counting_notes': ['已有说明']}))
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/audit_character_candidates.py'),
                                     '--database', str(database), '--characters', str(characters),
                                     '--output', str(output), '--target', '1'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            audit = json.loads(output.read_text())
            counts = {row['character']: row for row in audit['characters']}
            self.assertEqual([counts[ch]['ai_evidence_count'] for ch in '云山水月海'], [1, 1, 0, 0, 0])
            self.assertEqual([counts[ch]['provisional_count'] for ch in '云山水月海'], [2, 3, 1, 0, 0])
            self.assertEqual([counts[ch]['independent_screen_count'] for ch in '云山水月海'], [1, 1, 0, 0, 0])
            self.assertEqual(audit['summary']['independent_review']['remaining_deficit'], 3)
            self.assertEqual(audit['summary']['independent_review']['at_target'], 2)
            self.assertFalse(audit['summary']['independent_review']['final_acceptance'])
            self.assertTrue(all(row['final_quality_acceptance'] == 'unverified' for row in counts.values()))
            self.assertIn('已有说明', audit['counting_notes'])
            self.assertEqual(before, hashlib.sha256(database.read_bytes()).hexdigest())
