import json
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import review_legacy_candidates as legacy


class LegacyCandidateReviewTests(unittest.TestCase):
    def test_queue_excludes_reviewed_excluded_and_semantically_documented_names(self):
        # Accidentally requeueing prior decisions or treating missing metadata as
        # accepted quality must fail; this queue only prepares new evidence.
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'names.db'
            with sqlite3.connect(database) as connection:
                connection.execute('CREATE TABLE name_candidates (name TEXT, source_type TEXT, notes_json TEXT, status TEXT, name_length INTEGER)')
                for name, source, notes, status in [
                    ('暮云', 'ai', '[]', '待核验'),
                    ('清越', 'ai', '[]', '待核验'),
                    ('云山', 'ai', json.dumps(['质量初筛：各字语义角色={"云":"云气的自然意象","山":"山岳的自然意象"}；自然度自评=4/5']), '待核验'),
                    ('旧名', 'ai', '[]', '待核验'),
                    ('待定', 'ai', '[]', '质量复核待定'),
                    ('剔除', 'ai', '[]', '质量复核未通过'),
                    ('规则', 'rule', '[]', '待核验'),
                ]:
                    connection.execute('INSERT INTO name_candidates VALUES (?,?,?,?,?)', (name, source, notes, status, len(name)))
                connection.execute('CREATE TABLE candidate_quality_reviews (name TEXT, decision TEXT)')
                connection.execute('INSERT INTO candidate_quality_reviews VALUES (?,?)', ('旧名', 'keep'))
            self.assertEqual(legacy.select_legacy_names(database, set(), 24), ['暮云', '清越'])
            self.assertEqual(legacy.select_legacy_names(database, {'暮云'}, 1), ['清越'])

    def test_semantic_response_must_cover_exact_names_without_duplicates(self):
        valid = {'names': [{'name': '秋水', 'meaning': '秋日江湖之水',
                            'character_roles': {'秋': '秋季', '水': '江湖之水'}, 'naturalness': 3}]}
        self.assertEqual(legacy.validate_semantics(['秋水'], valid), valid['names'])
        for bad in [{'names': []}, {'names': valid['names'] * 2},
                    {'names': [{**valid['names'][0], 'name': '春水'}]},
                    {'names': [{**valid['names'][0], 'character_roles': {'水': '江湖之水'}}]},
                    {'names': [{**valid['names'][0], 'naturalness': True}]}]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                legacy.validate_semantics(['秋水'], bad)

    def test_only_completed_evidence_is_excluded_from_resume_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'good.json').write_text(json.dumps({'status': 'legacy_review_not_applied', 'names': ['秋水', '暮云']}))
            (root / 'failed.json').write_text(json.dumps({'status': 'legacy_review_failed', 'names': ['竹影']}))
            (root / 'partial.json').write_text('{')
            self.assertEqual(legacy.prepared_names(root), {'秋水', '暮云'})

    def test_preparation_preserves_database_and_resumes_after_saved_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / 'names.db'
            output = root / 'evidence'
            with sqlite3.connect(database) as connection:
                connection.execute('CREATE TABLE name_candidates (name TEXT, source_type TEXT, notes_json TEXT, status TEXT, name_length INTEGER)')
                connection.execute('INSERT INTO name_candidates VALUES (?,?,?,?,?)', ('秋水', 'ai', '[]', '待核验', 2))
            before = hashlib.sha256(database.read_bytes()).hexdigest()
            semantic = [{'name': '秋水', 'meaning': '秋日之水', 'character_roles': {'秋': '秋季限定', '水': '江湖之水'}, 'naturalness': 5}]
            reviews = [{'name': '秋水', 'decision': 'keep', 'reason': '自然水景'}]
            with patch.object(legacy.api_server, 'selected_ai_provider', return_value={'model': 'fixture'}), \
                 patch.object(legacy, 'load_character_names', return_value=[]), \
                 patch.object(legacy, 'request_semantics', return_value=(semantic, {})), \
                 patch.object(legacy, 'request_review', return_value=(reviews, {})):
                legacy.run(database, output, 24, 1, None)
            evidence = json.loads(next(output.glob('*.json')).read_text())
            self.assertEqual(evidence['semantics'][0]['name'], '秋水')
            self.assertEqual(evidence['reviews'][0]['decision'], 'keep')
            self.assertFalse(evidence['final_acceptance'])
            self.assertEqual(legacy.select_legacy_names(database, legacy.prepared_names(output), 24), [])
            self.assertEqual(before, hashlib.sha256(database.read_bytes()).hexdigest())

    def test_failed_semantics_preserves_successful_review_without_marking_prepared(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'evidence'
            database = Path(directory) / 'names.db'
            with sqlite3.connect(database) as connection:
                connection.execute('CREATE TABLE name_candidates (name TEXT, source_type TEXT, notes_json TEXT, status TEXT, name_length INTEGER)')
                connection.execute('INSERT INTO name_candidates VALUES (?,?,?,?,?)', ('井木', 'ai', '[]', '待核验', 2))
            reviews = [{'name': '井木', 'decision': 'uncertain', 'reason': '字义待核'}]
            with patch.object(legacy.api_server, 'selected_ai_provider', return_value={'model': 'fixture'}), \
                 patch.object(legacy, 'load_character_names', return_value=[]), \
                 patch.object(legacy, 'request_semantics', side_effect=ValueError('语义角色缺项')), \
                 patch.object(legacy, 'request_review', return_value=(reviews, {})):
                legacy.run(database, output, 24, 1, None)
            evidence = json.loads(next(output.glob('*.json')).read_text())
            self.assertEqual(evidence['status'], 'legacy_review_failed')
            self.assertEqual(evidence['reviews'], reviews)
            self.assertIn('语义角色缺项', evidence['errors']['semantics'])
            self.assertEqual(legacy.prepared_names(output), set())
