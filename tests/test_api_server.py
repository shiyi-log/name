import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import api_server


class QuietHandler(api_server.NamingRequestHandler):
    def log_message(self, *_args):
        pass


class GenerationRequestTests(unittest.TestCase):
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
            ):
                with self.subTest(payload=payload), self.assertRaises(ValueError):
                    api_server.generate_for_character(payload)
            request.assert_not_called()

    def test_generated_names_are_filtered_scored_and_saved_to_sqlite(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "names.db"
            characters = list("云山月川")
            tokens = {"actions": [], "images": [], "abstracts": []}
            real_build_database = api_server.build_database
            real_build_database(characters, tokens, [], database_path)

            def save_candidates(character_list, token_map, candidates):
                real_build_database(character_list, token_map, candidates, database_path)

            raw_names = [
                {"name": "云山", "meaning": "云与山"},
                {"name": "云山", "meaning": "重复候选"},
                {"name": "山月", "meaning": "没有锚字"},
                {"name": "云云", "meaning": "重复字"},
                {"name": "云月川", "meaning": "长度不符"},
                {"name": "云A", "meaning": "含非汉字"},
                {"name": "云川", "meaning": "排除名单"},
            ]
            with patch.object(api_server, "load_json", side_effect=[characters, tokens]), patch.object(
                api_server, "request_ai_names", return_value=raw_names
            ), patch.object(api_server, "excluded_names", return_value={"云川"}), patch.object(
                api_server, "load_existing_candidates", return_value=[]
            ), patch.object(api_server, "build_database", side_effect=save_candidates), patch.dict(
                api_server.os.environ, {"OPENAI_MODEL": "mock-model"}
            ):
                result = api_server.generate_for_character({"character": "云", "lengths": [2], "count": 5})

            self.assertEqual(result["added"], 1)
            self.assertEqual(result["rejected"], 6)
            self.assertEqual(result["items"][0]["name"], "云山")
            with closing(sqlite3.connect(database_path)) as connection:
                row = connection.execute(
                    "SELECT name, name_length, source_type, ai_model, total_score FROM name_candidates"
                ).fetchone()
            self.assertEqual(row[:4], ("云山", 2, "ai", "mock-model"))
            self.assertGreaterEqual(row[4], 0)
            self.assertLessEqual(row[4], 100)


if __name__ == "__main__":
    unittest.main()
