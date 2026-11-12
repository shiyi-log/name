import unittest
import tempfile
import json
import sqlite3
from pathlib import Path
from unittest.mock import patch
from scripts import generate_all_character_candidates as generation

from scripts.generate_all_character_candidates import (
    count_names_for_characters,
    count_names_for_character_set,
    is_transient_error,
    parse_character_roles,
    pending_targets,
    phase_targets,
    request_batch_size,
    targets_for_cycle,
    generation_complete,
    should_retry_idle_cycle,
    should_wait_for_other_ranges,
    select_character_range,
)


class CandidateBatchTests(unittest.TestCase):
    def test_generation_counts_exclude_legacy_and_incomplete_quality_evidence(self):
        roles = {'云': '云气形成自然意象', '山': '山岳形成自然意象'}
        def notes(character_roles, rating):
            return json.dumps(['质量初筛：各字语义角色=' + json.dumps(character_roles, ensure_ascii=False)
                               + '；自然度自评=' + str(rating) + '/5'], ensure_ascii=False)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'names.db'
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE name_candidates (name TEXT, name_length INTEGER, source_type TEXT, registry_note TEXT, notes_json TEXT, status TEXT DEFAULT '待核验')")
                db.executemany('INSERT INTO name_candidates (name,name_length,source_type,registry_note,notes_json) VALUES (?,2,?,?,?)', [
                    ('云水', 'rule', '', '[]'),
                    ('云山', 'ai', '', notes(roles, 5)),
                    ('云禾', 'ai', '', notes({'云': '云气形成自然意象'}, 5)),
                    ('云月', 'ai', '', notes({'云': '云气形成自然意象', '月': '月色形成自然意象'}, 2)),
                    ('云川', 'ai', '', json.dumps(['质量初筛：各字语义角色=' + json.dumps({'云': '云气形成自然意象', '川': '川流形成自然意象'})])),
                    ('山云', 'ai', '', notes(roles, 5)),
                ])
                db.execute("UPDATE name_candidates SET status='质量复核未通过' WHERE name='山云'")
            with patch.object(generation.api_server, 'DB_PATH', path):
                records = generation.load_character_names()
        counts = generation.count_names_for_character_set(records, list('云山水禾月川'))
        self.assertEqual(counts, {'云': 1, '山': 1, '水': 0, '禾': 0, '月': 0, '川': 0})

    def test_restart_skips_zero_yield_character_until_retry_deadline(self):
        attempted = []

        def generate(payload):
            attempted.append(payload['character'])
            return {'providerName': 'test', 'model': 'test', 'rejected': 0}

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            with patch.object(generation.api_server, 'load_json', return_value=list('丐山')), patch.object(generation, 'load_character_names', return_value=[]), patch.object(generation, 'request_with_retries', side_effect=generate), patch.object(generation.time, 'sleep'):
                generation.run(target=500, provider_id=None, limit=1, state_path=path, pause_seconds=0)
                generation.run(target=500, provider_id=None, limit=1, state_path=path, pause_seconds=0)
            state = generation.load_state(path)
        self.assertEqual(attempted, ['丐', '山'])
        self.assertEqual(set(state['retry_after']), {'丐', '山'})
        self.assertFalse(state['completed'])

    def test_worker_moves_past_failed_character_and_persists_retry_time(self):
        attempted = []
        def generate(payload):
            attempted.append(payload['character'])
            if payload['character'] == '丐':
                raise RuntimeError('AI 服务返回 HTTP 503')
            return {'providerName': 'test', 'model': 'test', 'rejected': 0}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            with patch.object(generation.api_server, 'load_json', return_value=list('丐山水')), patch.object(generation, 'load_character_names', return_value=[]), patch.object(generation, 'request_with_retries', side_effect=generate), patch.object(generation.time, 'sleep'):
                code = generation.run(target=500, provider_id=None, limit=2, state_path=path, pause_seconds=0)
            state = generation.load_state(path)
        self.assertEqual(attempted, ['丐', '山'])
        self.assertEqual(code, 2)
        self.assertFalse(state['completed'])
        self.assertGreater(state['retry_after']['丐'], 0)

    def test_deferred_character_does_not_block_other_targets(self):
        targets = [("丐", 1), ("山", 500), ("水", 500)]
        self.assertEqual(
            targets_for_cycle(targets, {"丐": 200}, now=100),
            [("山", 500), ("水", 500)],
        )

    def test_deferred_character_returns_after_cooldown(self):
        targets = [("丐", 1), ("山", 500)]
        self.assertEqual(targets_for_cycle(targets, {"丐": 200}, now=250), targets)

    def test_zero_candidate_characters_are_present_in_full_character_counts(self):
        counts = count_names_for_character_set(["云山"], list("云山水"))
        self.assertEqual(counts, {"云": 1, "山": 1, "水": 0})

    def test_counts_each_unique_name_once_per_character(self):
        counts = count_names_for_characters(["云山", "云山", "山河", "雨"])
        self.assertEqual(counts, {"云": 1, "山": 2, "河": 1, "雨": 1})

    def test_ai_names_only_count_characters_with_explicit_semantic_roles(self):
        records = [
            ("云山", "ai", {"云": "云气形成山水意象", "山": "山岳形成山水意象"}),
            ("云禾", "ai", {"云": "云气形成自然意象"}),
            ("云水", "rule", {}),
        ]
        counts = count_names_for_character_set(records, list("云山禾水"))
        self.assertEqual(counts, {"云": 3, "山": 1, "禾": 0, "水": 1})

    def test_role_parser_reads_json_evidence_with_trailing_naturalness(self):
        roles = {"云": "云气形成自然意象", "山": "山岳形成自然意象"}
        note = "质量初筛：各字语义角色=" + __import__("json").dumps(roles, ensure_ascii=False) + "；自然度自评=5/5"
        notes = __import__("json").dumps(["其他评分备注", note], ensure_ascii=False)
        self.assertEqual(parse_character_roles(notes), roles)

    def test_counts_are_reloaded_from_database_after_each_generated_batch(self):
        first_counts = count_names_for_characters(["云山"])
        refreshed_counts = count_names_for_characters(["云山", "云月"])
        self.assertEqual(first_counts["云"], 1)
        self.assertEqual(refreshed_counts["云"], 2)

    def test_pending_targets_include_all_characters_and_only_their_deficits(self):
        targets = pending_targets(["云", "山", "水"], {"云": 500, "山": 140}, 500)
        self.assertEqual(targets, [("水", 500), ("山", 360)])

    def test_mixed_phase_interleaves_uncovered_and_underfilled_characters(self):
        self.assertEqual(
            phase_targets(["云", "山", "水"], {"云": 1, "山": 0, "水": 4}, 500),
            ("coverage_and_fill", [("山", 500), ("云", 499), ("水", 496)]),
        )

    def test_uncovered_character_does_not_prevent_other_character_reaching_target(self):
        characters = list("云山水")
        counts = {"云": 500, "山": 0, "水": 4}
        phase, targets = phase_targets(characters, counts, 500)
        self.assertEqual(phase, "coverage_and_fill")
        self.assertEqual(targets, [("山", 500), ("水", 496)])

    def test_shard_fills_its_characters_while_other_shards_have_zeroes(self):
        phase, targets = phase_targets(
            ["云", "山"], {"云": 1, "山": 4, "水": 0}, 500,
            all_characters=["云", "山", "水"],
        )
        self.assertEqual(phase, "fill_to_500")
        self.assertEqual(targets, [("云", 499), ("山", 496)])

    def test_many_uncovered_characters_do_not_starve_fill_work(self):
        phase, targets = phase_targets(list('丐奸山水'), {'丐': 0, '奸': 0, '山': 490, '水': 2}, 500)
        self.assertEqual([character for character, _ in targets], list('丐水奸山'))
        self.assertEqual(set(targets), {('丐', 500), ('奸', 500), ('山', 10), ('水', 498)})

    def test_shard_cannot_report_global_completion_when_other_ranges_are_short(self):
        self.assertFalse(generation_complete({"云": 500, "山": 500, "水": 0}, ["云", "山", "水"], 500))

    def test_generation_completes_only_when_every_character_meets_target(self):
        self.assertTrue(generation_complete({"云": 500, "山": 520}, ["云", "山"], 500))
        self.assertFalse(generation_complete({"云": 500, "山": 499}, ["云", "山"], 500))

    def test_idle_worker_retries_until_global_target_is_met(self):
        self.assertTrue(should_retry_idle_cycle({"云": 1, "山": 0}, ["云", "山"], 500))
        self.assertFalse(should_retry_idle_cycle({"云": 500, "山": 500}, ["云", "山"], 500))

    def test_only_fully_filled_shard_waits_for_other_ranges(self):
        self.assertTrue(should_wait_for_other_ranges(
            {"云": 500, "山": 0}, ["云"], ["云", "山"], 500,
        ))
        self.assertFalse(should_wait_for_other_ranges(
            {"云": 1, "山": 0}, ["云"], ["云", "山"], 500,
        ))

    def test_fill_phase_starts_after_all_characters_have_coverage(self):
        characters = list("云山水")
        phase, targets = phase_targets(characters, {"云": 500, "山": 1, "水": 4}, 500)
        self.assertEqual(phase, "fill_to_500")
        self.assertEqual(targets, [("山", 499), ("水", 496)])

    def test_coverage_requests_a_quality_filtered_batch_for_more_accepted_names(self):
        self.assertEqual(request_batch_size("coverage", 1), 90)

    def test_fill_requests_only_the_remaining_deficit_up_to_provider_batch_limit(self):
        self.assertEqual(request_batch_size("fill_to_500", 12), 12)
        self.assertEqual(request_batch_size("fill_to_500", 45), 45)
        self.assertEqual(request_batch_size("fill_to_500", 500), 90)

    def test_provider_wrapped_timeout_is_transient(self):
        self.assertTrue(is_transient_error(RuntimeError("AI 服务连接失败：The read operation timed out")))
        self.assertFalse(is_transient_error(RuntimeError("AI 服务返回 HTTP 401")))

    def test_rate_limit_and_service_unavailable_are_transient(self):
        self.assertTrue(is_transient_error(RuntimeError("AI 服务返回 HTTP 429")))
        self.assertTrue(is_transient_error(RuntimeError("AI 服务返回 HTTP 503")))
        self.assertTrue(is_transient_error(RuntimeError("AI 服务返回 HTTP 504")))
        self.assertFalse(is_transient_error(RuntimeError("AI 服务返回 HTTP 400")))

    def test_character_range_uses_start_inclusive_end_exclusive_indexes(self):
        self.assertEqual(select_character_range(list("甲乙丙丁"), 1, 3), ["乙", "丙"])


if __name__ == "__main__":
    unittest.main()
