import json
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import review_character_candidates as review


class CandidateReviewTests(unittest.TestCase):
    def test_comparison_context_includes_related_kept_names_but_excludes_targets_and_rejections(self):
        records = [(name, 'ai', {}) for name in ['枕水', '枕秋水', '枕山', '枕青山', '青禾', '枕海']]
        decisions = {'枕水': 'keep', '枕秋水': 'keep', '枕青山': 'reject', '青禾': 'keep', '枕海': 'uncertain'}
        related = review.comparison_names(['枕山'], records, decisions)
        self.assertEqual(related['枕'], ['枕水', '枕秋水'])
        self.assertNotIn('山', related)
        self.assertNotIn('青禾', [name for names in related.values() for name in names])
        self.assertEqual(review.comparison_names(['枕山'], records, decisions, per_character=1)['枕'], ['枕水'])

    def test_comparison_limit_does_not_hide_closest_older_variant(self):
        records = [(name, 'ai', {}) for name in ['枕春风', '枕长风', '枕星河', '枕石', '枕水']]
        decisions = {name: 'keep' for name, _, _ in records}
        self.assertEqual(review.comparison_names(['枕秋水'], records, decisions, per_character=1), {'枕': ['枕水'], '水': ['枕水']})

    def test_phrase_dictionary_lookup_requires_exact_form_and_preserves_senses(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'entries.json'
            path.write_text(json.dumps([
                {'title': '倓然', 'definition': '安然不疑的樣子。', 'row': 9},
                {'title': '倓然', 'definition': '另一讀音。', 'row': 10},
                {'title': '青雲', 'definition': '繁體原文。', 'row': 11},
            ]))
            result = review.dictionary_phrase_references(['倓然', '青云', '新造词'], path)
        self.assertEqual([entry['definition'] for entry in result['倓然']], ['安然不疑的樣子。', '另一讀音。'])
        self.assertEqual(result['青云'], [])
        self.assertEqual(result['新造词'], [])

    def test_review_shards_partition_names_without_overlap(self):
        records = [(name, 'ai', {}) for name in ['云山', '云水', '云月', '山川', '月白', '星河']]
        partitions = [review.select_review_names(records, {'云水'}, 24, index, 4) for index in range(4)]
        flattened = [name for partition in partitions for name in partition]
        self.assertEqual(sorted(flattened), ['云山', '云月', '山川', '星河', '月白'])
        self.assertEqual(len(flattened), len(set(flattened)))
        extended = [('新月', 'ai', {})] + records
        for index, names in enumerate(partitions):
            self.assertTrue(set(names) <= set(review.select_review_names(extended, {'云水'}, 24, index, 4)))

    def test_review_selection_excludes_reviewed_before_applying_batch_limit(self):
        records = [(name, 'ai', {}) for name in ['云山', '云水', '云月']]
        self.assertEqual(review.select_review_names(records, {'云山'}, 1, 0, 1), ['云水'])
        for index, count in [(-1, 4), (4, 4), (0, 0)]:
            with self.subTest(index=index, count=count), self.assertRaises(ValueError):
                review.select_review_names(records, set(), 24, index, count)

    def test_longer_phrase_does_not_hide_shorter_phrase_context(self):
        contexts = review.phrase_references(['蔽芾', '蔽芾甘棠'], corpus={'诗经': '蔽芾甘棠，勿翦勿伐。'})
        self.assertEqual(contexts['蔽芾'], [{'book': '诗经', 'line': 1, 'context': '蔽芾甘棠，勿翦勿伐。'}])
        self.assertEqual(contexts['蔽芾甘棠'], contexts['蔽芾'])

    def test_phrase_references_preserve_real_context_and_do_not_require_a_match(self):
        contexts = review.phrase_references(['壅培', '蔽芾', '现代新名'], corpus={
            '宋词': '前句结束。幽葩细萼，小园低槛，壅培未就。后句开始。',
            '诗经': '蔽芾甘棠，勿翦勿伐。\n蔽芾甘棠，勿翦勿败。',
        })
        self.assertEqual(contexts['壅培'][0]['book'], '宋词')
        self.assertEqual(contexts['壅培'][0]['context'], '幽葩细萼，小园低槛，壅培未就。')
        self.assertEqual(contexts['蔽芾'][0]['line'], 1)
        self.assertEqual(contexts['现代新名'], [])
        self.assertLessEqual(len(contexts['蔽芾']), 2)

    def test_progress_reports_independent_screen_separately_from_unreviewed_names(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'names.db'
            with sqlite3.connect(path) as db:
                review.ensure_review_table(db)
                db.executemany('INSERT INTO candidate_quality_reviews (name,review_id,model,decision,reason) VALUES (?,?,?,?,?)', [
                    ('云山', 'a', 'model', 'keep', '自然'),
                    ('云山', 'b', 'model', 'keep', '自然'),
                    ('云月', 'a', 'model', 'uncertain', '存疑'),
                ])
            records = [('云山', 'ai', {'云': '云气形成自然意象', '山': '山岳形成自然意象'}),
                       ('云水', 'ai', {'云': '云气形成自然意象', '水': '水面形成自然意象'})]
            report = review.review_progress(path, records, list('云山水月'), 500)
            self.assertEqual(report['reviewed_names'], 2)
            self.assertEqual(report['kept_name_count'], 1)
            self.assertEqual(report['unreviewed_eligible_names'], 1)
            self.assertEqual(report['independent_screen_counts'], {'云': 1, '山': 1, '水': 0, '月': 0})
            self.assertFalse(report['final_acceptance'])

    def test_independent_review_uses_each_provider_protocol_and_validates_names(self):
        decisions = [{'name': '云山', 'decision': 'keep', 'reason': '云山相映意象自然'}]
        for wire in ['responses', 'chat_completions']:
            result = ({'output': [{'content': [{'type': 'output_text', 'text': json.dumps({'reviews': decisions})}]}]}
                      if wire == 'responses' else {'choices': [{'message': {'content': json.dumps({'reviews': decisions})}}]})
            provider = {'wireApi': wire, 'model': 'fixture-model', 'baseUrl': 'https://example.test/v1', '_api_key': 'fixture-key'}
            with self.subTest(wire=wire), patch.object(review.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps(result).encode())) as request:
                reviews, prompt = review.request_review(['云山'], provider)
                self.assertEqual(reviews, decisions)
                sent = json.loads(request.call_args.args[0].data)
                self.assertEqual(sent['model'], 'fixture-model')
                self.assertIn('input' if wire == 'responses' else 'messages', sent)
                self.assertEqual(prompt['names'], ['云山'])
                self.assertEqual(prompt['direct_dictionary']['云']['definition'], 'say, speak; clouds')
                self.assertNotIn('pitch', prompt)
                self.assertNotIn('naturalness', prompt)

    def test_incomplete_or_unknown_results_cannot_update_candidates(self):
        with self.assertRaises(ValueError):
            review.validate_reviews(['云山', '云月'], {'reviews': [{'name': '云山', 'decision': 'keep', 'reason': '语义自然'}]})
        with self.assertRaises(ValueError):
            review.validate_reviews(['云山'], {'reviews': [{'name': '山月', 'decision': 'keep', 'reason': '语义自然'}]})
        with self.assertRaises(ValueError):
            review.validate_reviews(['云山'], {'reviews': [{'name': '云山', 'decision': 'keep', 'reason': ''}]})

    def test_review_persists_decisions_and_excludes_uncertain_without_claiming_acceptance(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'names.db'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE name_candidates (name TEXT PRIMARY KEY, status TEXT, registry_note TEXT)')
                db.executemany('INSERT INTO name_candidates VALUES (?, ?, ?)', [('云山', '待核验', '原说明'), ('站上潮头', '待核验', '原说明'), ('㙍境', '待核验', '原说明')])
            reviews = review.validate_reviews(['云山', '站上潮头', '㙍境'], {'reviews': [
                {'name': '云山', 'decision': 'keep', 'reason': '云山相映的自然意象'},
                {'name': '站上潮头', 'decision': 'reject', 'reason': '完整口号，名称自然度不足'},
                {'name': '㙍境', 'decision': 'uncertain', 'reason': '地名关系缺乏背景依据'},
            ]})
            review.save_reviews(path, reviews, 'fixture-review', 'model')
            with sqlite3.connect(path) as db:
                rows = dict(db.execute('SELECT name, status FROM name_candidates'))
                self.assertEqual(rows, {'云山': '待核验', '站上潮头': '质量复核未通过', '㙍境': '质量复核待定'})
                self.assertEqual(db.execute('SELECT COUNT(*) FROM candidate_quality_reviews').fetchone()[0], 3)
                self.assertEqual(db.execute('SELECT registry_note FROM name_candidates WHERE name=?', ('云山',)).fetchone()[0], '原说明')

    def test_review_does_not_override_existing_manual_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'names.db'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE name_candidates (name TEXT PRIMARY KEY, status TEXT, registry_note TEXT)')
                db.execute("INSERT INTO name_candidates VALUES ('云山','质量复核未通过','人工否决')")
            review.save_reviews(path, [{'name': '云山', 'decision': 'keep', 'reason': '模型认为可保留'}], 'fixture', 'model')
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute('SELECT status,registry_note FROM name_candidates').fetchone(), ('质量复核未通过', '人工否决'))
