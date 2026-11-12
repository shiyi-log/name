import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import ExitStack, closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import api_server


class CharacterNameValidationTests(unittest.TestCase):
    def test_character_definition_has_source_without_inventing_missing_senses(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'definitions.json'
            path.write_text(json.dumps({'source_url': 'https://unicode.org/fixture', 'version': '17.0.0',
                                        'characters': {'𬜯': {'definition': 'name of a variety of grass', 'mandarin': 'liǎng'},
                                                       '𫇭': {'mandarin': 'wěi'}}}), encoding='utf-8')
            with patch.object(api_server, 'CHARACTER_DEFINITIONS_PATH', path):
                api_server._character_definitions.cache_clear()
                definition = api_server.character_definition_context('𬜯')
                self.assertEqual(definition['definition'], 'name of a variety of grass')
                self.assertEqual(definition['source_url'], 'https://unicode.org/fixture')
                self.assertEqual(definition['mandarin'], 'liǎng')
                self.assertNotIn('definition', api_server.character_definition_context('𫇭'))
                self.assertIsNone(api_server.character_definition_context('无'))
            api_server._character_definitions.cache_clear()

    def test_known_definition_error_cannot_be_admitted_by_high_self_rating(self):
        self.assertFalse(api_server.has_quality_evidence({
            'character_roles': {'𫇭': '指生长在水边的芦苇', '洲': '指水中陆地形成地貌'},
            'naturalness': 5,
        }, '𫇭洲'))
        self.assertTrue(api_server.has_quality_evidence({
            'character_roles': {'𫇭': '指芡的茎，形成水生植物意象', '洲': '指水中陆地形成地貌'},
            'naturalness': 4,
        }, '𫇭洲'))
        self.assertTrue(api_server.has_quality_evidence({
            'character_roles': {'苇': '指芦苇，形成水岸植物意象', '洲': '指水中陆地形成地貌'},
            'naturalness': 4,
        }, '苇洲'))

    def test_grass_character_cannot_be_reinterpreted_as_eyesight(self):
        self.assertFalse(api_server.has_quality_evidence({
            'character_roles': {'𬜯': '承载目光开阔、视见舒展的意象', '明': '明亮构成光影清晰的意象'},
            'naturalness': 5,
        }, '𬜯明'))
        self.assertTrue(api_server.has_quality_evidence({
            'character_roles': {'𬜯': '一种草名，形成自然草木的意象', '明': '明亮构成光影清晰的意象'},
            'naturalness': 4,
        }, '𬜯明'))

    def test_anchor_character_is_exempt_from_rare_character_avoidance(self):
        guidance = api_server.character_frequency_guidance("蠼")
        self.assertIn("锚定字“蠼”是本次命名核心，即使字频低或字形生僻也必须自然参与语义", guidance)
        self.assertIn("其他配字优先使用常见、易读且语义自然的汉字", guidance)

    def test_reference_context_is_limited_and_source_labeled(self):
        contexts = api_server.reference_contexts_for_character(
            "云", corpus={"诗经": "南有樛木，云气蔼蔼。", "楚辞": "云中君兮"}, max_per_book=1
        )
        self.assertEqual(len(contexts), 2)
        self.assertTrue(all(item["book"] in {"诗经", "楚辞"} for item in contexts))
        self.assertTrue(all("云" in item["context"] for item in contexts))

    def test_reference_context_ignores_books_without_anchor_character(self):
        self.assertEqual(
            api_server.reference_contexts_for_character("云", corpus={"楚辞": "山有木兮"}),
            [],
        )

    def test_reference_context_stays_within_sentence_around_anchor(self):
        contexts = api_server.reference_contexts_for_character(
            "云", corpus={"周易": "前句结束。云行雨施，品物流形。后句开始。"}
        )
        self.assertEqual(len(contexts), 1)
        self.assertEqual(contexts[0]["context"], "云行雨施，品物流形。")

    def test_full_character_list_allows_extension_cjk_without_allowing_punctuation(self):
        allowed = {"𫚭", "山"}
        self.assertTrue(api_server.is_character_name("𫚭山", allowed))
        self.assertFalse(api_server.is_character_name("𫚭!", allowed))
        self.assertFalse(api_server.is_character_name("𫚭A", allowed))

    def test_candidate_quality_requires_roles_for_every_character_and_high_self_rating(self):
        self.assertTrue(api_server.has_quality_evidence({
            "character_roles": {"云": "云气构成自然意象", "山": "山岳构成自然意象"}, "naturalness": 5,
        }, "云山"))
        self.assertFalse(api_server.has_quality_evidence({
            "character_roles": {"云": "云气构成自然意象"}, "naturalness": 5,
        }, "云山"))
        self.assertFalse(api_server.has_quality_evidence({
            "character_roles": {"云": "云气构成自然意象", "山": "作为装饰字"}, "naturalness": 5,
        }, "云山"))
        self.assertFalse(api_server.has_quality_evidence({
            "character_roles": {"云": "云气构成自然意象", "山": "山岳构成自然意象"}, "naturalness": 2,
        }, "云山"))
        self.assertFalse(api_server.has_quality_evidence({"meaning": "云与山"}))


class QuietHandler(api_server.NamingRequestHandler):
    def log_message(self, *_args):
        pass


class GenerationRequestTests(unittest.TestCase):
    def setUp(self):
        self.contexts = ExitStack()
        self.addCleanup(self.contexts.close)
        directory = self.contexts.enter_context(tempfile.TemporaryDirectory())
        self.contexts.enter_context(patch.object(api_server, "CC_SWITCH_DB", Path(directory) / "missing.db"))
        self.contexts.enter_context(patch.object(api_server, "AI_CONFIG_PATH", Path(directory) / "ai-providers.json"))
        self.contexts.enter_context(patch.object(api_server, "_selected_provider_id", None))
        self.contexts.enter_context(patch.dict(api_server.os.environ, {}, clear=True))

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def post(self, body=b"{}", headers=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        try:
            connection.request(
                "POST", "/api/generate/by-character", body=body,
                headers={"Content-Type": "application/json", **(headers or {})},
            )
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_cross_origin_requests_do_not_generate(self):
        with patch.object(api_server, "generate_for_character") as generate:
            for headers in (
                {"Origin": "https://other.example"},
                {"Sec-Fetch-Site": "cross-site"},
                {"Origin": "null"},
            ):
                with self.subTest(headers=headers):
                    self.assertEqual(self.post(headers=headers)[0], 403)
            generate.assert_not_called()

    def test_plain_text_requests_do_not_generate(self):
        with patch.object(api_server, "generate_for_character") as generate:
            self.assertEqual(self.post(headers={"Content-Type": "text/plain"})[0], 415)
            generate.assert_not_called()

    def test_invalid_bodies_return_bad_request_without_generation(self):
        with patch.object(api_server, "generate_for_character") as generate:
            for body in (b"[]", b"null", b"\"name\"", b"{invalid"):
                with self.subTest(body=body):
                    self.assertEqual(self.post(body)[0], 400)
            self.assertEqual(self.post(b"", {"Content-Length": "-1"})[0], 400)
            self.assertEqual(self.post(b"", {"Content-Length": "16385"})[0], 413)
            generate.assert_not_called()

    def test_same_origin_json_request_reaches_generation(self):
        payload = {"character": "云", "lengths": [2, 3], "count": 5}
        expected = {"items": [], "added": 0, "model": "mock-model", "rejected": 0}
        host, port = self.server.server_address
        with patch.object(api_server, "generate_for_character", return_value=expected) as generate:
            status, result = self.post(
                json.dumps(payload).encode(), {"Origin": f"http://{host}:{port}"},
            )
            self.assertEqual((status, result), (200, expected))
            generate.assert_called_once_with(payload)

    def test_invalid_generation_fields_never_call_provider(self):
        with patch.object(api_server, "request_ai_names") as request, patch.object(
            api_server, "load_json", return_value=list("云山")
        ):
            for payload in (
                [], {"character": []}, {"character": "云山"},
                {"character": "云", "lengths": "2"},
                {"character": "云", "lengths": None},
                {"character": "云", "lengths": [2.5]},
                {"character": "云", "lengths": [True]},
                {"character": "云", "count": None},
                {"character": "云", "total_count": 10001},
                {"character": "云", "total_count": True},
            ):
                with self.subTest(payload=payload), self.assertRaises(ValueError):
                    api_server.generate_for_character(payload)
            request.assert_not_called()

    def test_extended_cjk_anchor_is_accepted_from_complete_character_list(self):
        supplementary_character = "𫚭"
        self.assertEqual(len(supplementary_character), 1)
        with patch.object(api_server, "load_json", return_value=[supplementary_character]), patch.object(
            api_server, "selected_ai_provider", return_value=None
        ), patch.object(api_server, "request_ai_names") as request:
            with self.assertRaisesRegex(RuntimeError, "尚未从 CC Switch"):
                api_server.generate_for_character({"character": supplementary_character, "count": 1})
            request.assert_not_called()


class CandidateCursorTests(unittest.TestCase):
    def setUp(self):
        self.contexts = ExitStack()
        self.addCleanup(self.contexts.close)
        directory = self.contexts.enter_context(tempfile.TemporaryDirectory())
        self.contexts.enter_context(patch.object(api_server, "CC_SWITCH_DB", Path(directory) / "missing.db"))
        self.contexts.enter_context(patch.object(api_server, "AI_CONFIG_PATH", Path(directory) / "ai-providers.json"))
        self.contexts.enter_context(patch.object(api_server, "_selected_provider_id", None))
        self.contexts.enter_context(patch.dict(api_server.os.environ, {}, clear=True))

    def test_keyset_pages_keep_total_and_do_not_repeat_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "names.db"
            with closing(sqlite3.connect(database_path)) as connection:
                connection.execute("""CREATE TABLE name_candidates (
                    id TEXT, name TEXT, name_length INTEGER, left_token TEXT, right_token TEXT,
                    template TEXT, direction TEXT, pitch TEXT, source TEXT, source_type TEXT,
                    ai_model TEXT, tags_json TEXT, scores_json TEXT, total_score REAL,
                    reasons_json TEXT, notes_json TEXT, status TEXT, registry_note TEXT
                )""")
                for name, score in (("云山", 90.0), ("云月", 85.0), ("云川", 80.0)):
                    connection.execute(
                        "INSERT INTO name_candidates VALUES (?, ?, 2, ?, ?, 'test', '东方', '意象', '本地', 'rules', '', ?, ?, ?, ?, ?, '待核验', '')",
                        (name, name, name[0], name[1], json.dumps([]), json.dumps({
                            "brand": 8, "readability": 8, "poetry": 8,
                            "distinctiveness": 8, "registration": 8,
                        }), score, json.dumps([]), json.dumps([])),
                    )
                connection.commit()
            with patch.object(api_server, "DB_PATH", database_path):
                first = api_server.query_candidates({"character": ["云"], "limit": ["2"]})
                last = first["items"][-1]
                second = api_server.query_candidates({
                    "character": ["云"], "limit": ["2"],
                    "after": [json.dumps({"name": last["name"], "score": last["score"], "scores": last["scores"]})],
                })
            self.assertEqual(first["total"], 3)
            self.assertTrue(first["hasMore"])
            self.assertFalse(second["hasMore"])
            self.assertEqual([item["name"] for item in first["items"] + second["items"]], ["云山", "云月", "云川"])

    def test_generated_names_are_filtered_scored_and_saved_to_sqlite(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "names.db"
            characters = list("云山月川")
            tokens = {"actions": [], "images": [], "abstracts": []}
            real_build_database = api_server.build_database
            real_build_database(characters, tokens, [], database_path)

            def save_candidates(character_list, token_map, candidates, *, db_path):
                self.assertEqual(db_path, database_path)
                real_build_database(character_list, token_map, candidates, db_path)

            raw_names = [
                {"name": "云山", "meaning": "云与山", "character_roles": {"云": "云气与山岳构成清楚自然的山水意象", "山": "山岳与云气构成清楚自然的山水意象"}, "naturalness": 5},
                {"name": "云山", "meaning": "重复候选"},
                {"name": "山月", "meaning": "没有锚字"},
                {"name": "云云", "meaning": "重复字"},
                {"name": "云月川", "meaning": "长度不符"},
                {"name": "云A", "meaning": "含非汉字"},
                {"name": "云川", "meaning": "排除名单"},
                {"name": "云禾", "meaning": "云字只是机械加在通用名词前", "character_roles": {"云": "作为品牌字前缀", "禾": "禾苗象征成长"}, "naturalness": 5},
                {"name": "云湖", "meaning": "云与湖相映", "character_roles": {"云": "云气映湖构成山水画面", "湖": "湖面承接云影"}, "naturalness": 2},
            ]
            with patch.object(api_server, "load_json", side_effect=[characters, tokens]), patch.object(
                api_server, "request_ai_names", return_value=raw_names
            ), patch.object(api_server, "excluded_names", return_value={"云川"}), patch.object(
                api_server, "load_existing_candidates", return_value=[]
            ), patch.object(api_server, "DB_PATH", database_path
            ), patch.object(api_server, "build_database", side_effect=save_candidates), patch.object(
                api_server, "PROVIDER_MODEL", "mock-model"
            ), patch.dict(
                api_server.os.environ, {"OPENAI_MODEL": "mock-model", "OPENAI_API_KEY": "test-token"}
            ):
                result = api_server.generate_for_character({"character": "云", "lengths": [2], "count": 5})

            self.assertEqual(result["added"], 1)
            self.assertEqual(result["rejected"], 8)
            self.assertEqual(result["items"][0]["name"], "云山")
            with closing(sqlite3.connect(database_path)) as connection:
                row = connection.execute(
                    "SELECT name, name_length, source_type, ai_model, total_score FROM name_candidates"
                ).fetchone()
            self.assertEqual(row[:4], ("云山", 2, "ai", "mock-model"))
            self.assertGreaterEqual(row[4], 0)
            self.assertLessEqual(row[4], 100)

    def test_total_generation_count_is_filled_in_bounded_unique_batches(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "names.db"
            characters = list("云山月川")
            tokens = {"actions": [], "images": [], "abstracts": []}
            api_server.build_database(characters, tokens, [], database_path)
            batches = [
                [{"name": "云山", "meaning": "云与山", "character_roles": {"云": "云气形成自然意象", "山": "山岳形成自然意象"}, "naturalness": 5}, {"name": "云月", "meaning": "云月相映", "character_roles": {"云": "云气形成自然意象", "月": "月色形成自然意象"}, "naturalness": 5}],
                [{"name": "云川", "meaning": "云过山川", "character_roles": {"云": "云气衬托川流意象", "川": "川流形成自然意象"}, "naturalness": 5}],
            ]
            with patch.object(api_server, "DB_PATH", database_path), patch.object(
                api_server, "load_json", side_effect=[characters, tokens]
            ), patch.object(api_server, "excluded_names", return_value=set()), patch.object(
                api_server, "load_existing_candidates", return_value=[]
            ), patch.object(api_server, "request_ai_names", side_effect=batches) as request, patch.object(
                api_server, "build_database", wraps=api_server.build_database
            ), patch.dict(
                api_server.os.environ, {"OPENAI_API_KEY": "test-token", "OPENAI_MODEL": "mock-model"}
            ):
                result = api_server.generate_for_character({
                    "character": "云", "lengths": [2], "total_count": 3,
                })

            self.assertEqual(result["added"], 3)
            self.assertEqual(request.call_count, 2)
            self.assertTrue(all(call.kwargs["requested_counts"][2] <= 80 for call in request.call_args_list))
            self.assertEqual(request.call_args_list[1].kwargs["avoid_names"], ["云山", "云月"])
            with closing(sqlite3.connect(database_path)) as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM name_candidates").fetchone()[0], 3)

    def test_total_generation_retries_late_missing_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "names.db"
            characters = list("云山月川")
            tokens = {"actions": [], "images": [], "abstracts": []}
            api_server.build_database(characters, tokens, [], database_path)
            batches = [
                [{"name": "云山", "meaning": "云与山", "character_roles": {"云": "云气形成自然意象", "山": "山岳形成自然意象"}, "naturalness": 5}],
                [],
                [],
                [{"name": "云川", "meaning": "云过山川", "character_roles": {"云": "云气衬托川流意象", "川": "川流形成自然意象"}, "naturalness": 5}],
            ]
            with patch.object(api_server, "DB_PATH", database_path), patch.object(
                api_server, "load_json", side_effect=[characters, tokens]
            ), patch.object(api_server, "excluded_names", return_value=set()), patch.object(
                api_server, "load_existing_candidates", return_value=[]
            ), patch.object(api_server, "request_ai_names", side_effect=batches) as request, patch.dict(
                api_server.os.environ, {"OPENAI_API_KEY": "test-token"}
            ):
                result = api_server.generate_for_character({
                    "character": "云", "lengths": [2], "total_count": 2,
                })

            self.assertEqual(result["added"], 2)
            self.assertEqual(request.call_count, 4)

    def test_later_timeout_preserves_already_screened_candidates_in_database(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / 'names.db'
            characters = list('云山')
            tokens = {'actions': [], 'images': [], 'abstracts': []}
            api_server.build_database(characters, tokens, [], database_path)
            candidate = {'name': '云山', 'meaning': '云气环绕山岳',
                         'character_roles': {'云': '云气形成自然意象', '山': '山岳形成自然意象'},
                         'naturalness': 5}
            with patch.object(api_server, 'DB_PATH', database_path), patch.object(
                api_server, 'load_json', side_effect=[characters, tokens]
            ), patch.object(api_server, 'excluded_names', return_value=set()), patch.object(
                api_server, 'request_ai_names', side_effect=[[candidate], RuntimeError('AI 服务连接失败：timed out')]
            ), patch.dict(api_server.os.environ, {'OPENAI_API_KEY': 'test-token'}):
                result = api_server.generate_for_character({'character': '云', 'lengths': [2], 'total_count': 2})
            self.assertEqual(result['added'], 1)
            self.assertIn('timed out', result['warning'])
            with closing(sqlite3.connect(database_path)) as connection:
                self.assertEqual(connection.execute('SELECT name FROM name_candidates').fetchall(), [('云山',)])

    def test_first_round_timeout_remains_an_error_for_retry(self):
        with patch.object(api_server, 'load_json', return_value=list('云山')), patch.object(
            api_server, 'load_existing_candidates', return_value=[]
        ), patch.object(api_server, 'excluded_names', return_value=set()), patch.object(
            api_server, 'request_ai_names', side_effect=RuntimeError('AI 服务连接失败：timed out')
        ), patch.dict(api_server.os.environ, {'OPENAI_API_KEY': 'test-token'}):
            with self.assertRaisesRegex(RuntimeError, 'timed out'):
                api_server.generate_for_character({'character': '云', 'lengths': [2], 'total_count': 2})


if __name__ == "__main__":
    unittest.main()
