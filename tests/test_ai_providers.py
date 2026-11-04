import hashlib
import http.client
import io
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


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.contexts = ExitStack()
        self.addCleanup(self.contexts.close)
        directory = self.contexts.enter_context(tempfile.TemporaryDirectory())
        self.provider_db = Path(directory) / "cc switch?#.db"
        self.contexts.enter_context(patch.object(api_server, "CC_SWITCH_DB", self.provider_db))
        self.contexts.enter_context(patch.object(api_server, "_selected_provider_id", None))
        self.contexts.enter_context(patch.dict(api_server.os.environ, {}, clear=True))

    def create_providers(self):
        with closing(sqlite3.connect(self.provider_db)) as connection:
            connection.execute("""CREATE TABLE providers (
                id TEXT, app_type TEXT, name TEXT, settings_config TEXT,
                website_url TEXT, sort_index INTEGER, is_current INTEGER)""")
            for identifier, wire_api, current in (("chat", "chat_completions", 0), ("response", "responses", 1)):
                settings = {
                    "auth": {"OPENAI_API_KEY": "test-secret-" + identifier},
                    "config": f'model = "model-{identifier}"\nbase_url = "https://{identifier}.example/v1"\nwire_api = "{wire_api}"',
                }
                connection.execute("INSERT INTO providers VALUES (?, 'codex', ?, ?, '', 0, ?)", (
                    identifier, identifier + " provider", json.dumps(settings), current,
                ))
            connection.execute("INSERT INTO providers VALUES ('oauth', 'codex', 'OAuth only', '{}', '', 1, 0)")
            connection.execute("INSERT INTO providers VALUES ('website-only', 'codex', 'No API endpoint', ?, 'https://website.example', 1, 0)", (
                json.dumps({"auth": {"OPENAI_API_KEY": "test-website-token"}}),
            ))
            connection.commit()

    def test_cc_switch_load_is_read_only_and_public_api_excludes_secrets(self):
        self.create_providers()
        before = hashlib.sha256(self.provider_db.read_bytes()).hexdigest()
        providers = api_server.load_cc_switch_providers()
        self.assertEqual([provider["id"] for provider in providers], ["response", "chat"])
        self.assertEqual(hashlib.sha256(self.provider_db.read_bytes()).hexdigest(), before)
        public = api_server.ai_providers()
        self.assertEqual(public["selectedId"], "response")
        self.assertEqual(public["items"][0]["wireApi"], "responses")
        self.assertNotIn("test-secret", json.dumps(public))
        self.assertNotIn("_api_key", json.dumps(public))
        self.assertNotIn("baseUrl", public["items"][0])

    def test_environment_and_explicit_selection_use_expected_provider(self):
        self.create_providers()
        with patch.dict(api_server.os.environ, {"OPENAI_API_KEY": "test-env", "OPENAI_MODEL": "env-model"}):
            self.assertEqual(api_server.selected_ai_provider()["id"], "environment")
            self.assertEqual(api_server.selected_ai_provider("chat")["model"], "model-chat")
            with self.assertRaises(ValueError):
                api_server.selected_ai_provider("missing")
        with patch.object(api_server, "_selected_provider_id", "chat"):
            self.assertEqual(api_server.ai_status()["providerId"], "chat")
        with patch.object(api_server, "_selected_provider_id", "removed"):
            self.assertFalse(api_server.ai_status()["configured"])

    def test_missing_configuration_does_not_send_request(self):
        with patch.object(api_server.urllib.request, "urlopen") as request:
            self.assertFalse(api_server.ai_status()["configured"])
            self.assertEqual(api_server.ai_providers(), {"items": [], "selectedId": ""})
            with self.assertRaises(RuntimeError):
                api_server.request_ai_names("云", 1, [2])
            request.assert_not_called()

    def test_chat_and_responses_requests_use_selected_endpoint_model_and_output(self):
        self.create_providers()
        names = [{"name": "云山", "meaning": "云与山"}]
        for provider_id, result in (
            ("chat", {"choices": [{"message": {"content": json.dumps({"names": names})}}]}),
            ("response", {"output": [{"content": [{"type": "output_text", "text": json.dumps({"names": names})}]}]}),
        ):
            with self.subTest(provider=provider_id), patch.object(
                api_server.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(result).encode())
            ) as request:
                self.assertEqual(api_server.request_ai_names("云", 1, [2], provider_id), names)
                outgoing = request.call_args.args[0]
                body = json.loads(outgoing.data)
                self.assertEqual(body["model"], "model-" + provider_id)
                suffix = "/responses" if provider_id == "response" else "/chat/completions"
                self.assertEqual(outgoing.full_url, f"https://{provider_id}.example/v1{suffix}")
                self.assertEqual(outgoing.get_header("Authorization"), "Bearer test-secret-" + provider_id)
                self.assertIn("input" if provider_id == "response" else "messages", body)

    def test_selected_provider_model_is_saved_with_generated_candidates(self):
        self.create_providers()
        database_path = self.provider_db.parent / "names.db"
        characters = list("云山月")
        tokens = {"actions": [], "images": [], "abstracts": []}
        api_server.build_database(characters, tokens, [], database_path)
        with patch.object(api_server, "DB_PATH", database_path), patch.object(
            api_server, "load_json", side_effect=[characters, tokens]
        ), patch.object(api_server, "excluded_names", return_value=set()), patch.object(
            api_server, "request_ai_names", return_value=[{"name": "云山", "meaning": "云与山"}]
        ) as request:
            result = api_server.generate_for_character({"character": "云", "lengths": [2], "providerId": "chat"})
        self.assertEqual(request.call_args.kwargs["provider"]["id"], "chat")
        self.assertEqual((result["providerId"], result["model"]), ("chat", "model-chat"))
        with closing(sqlite3.connect(database_path)) as connection:
            source, model = connection.execute("SELECT source, ai_model FROM name_candidates").fetchone()
        self.assertEqual(model, "model-chat")
        self.assertIn("chat provider", source)
        with patch.object(api_server, "load_json", return_value=characters), patch.object(
            api_server, "request_ai_names"
        ) as request:
            with self.assertRaises(ValueError):
                api_server.generate_for_character({"character": "云", "providerId": "removed"})
            request.assert_not_called()

    def test_provider_route_returns_frontend_contract(self):
        self.create_providers()

        class QuietHandler(api_server.NamingRequestHandler):
            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection(*server.server_address, timeout=2)
            try:
                connection.request("GET", "/api/ai/providers")
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                payload = json.loads(response.read())
                self.assertEqual(payload["selectedId"], "response")
                self.assertEqual(len(payload["items"]), 2)
                with patch.object(api_server.urllib.request, "urlopen") as request:
                    connection.request(
                        "POST", "/api/generate/by-character",
                        body=json.dumps({"character": "云", "providerId": "removed"}),
                        headers={"Content-Type": "application/json"},
                    )
                    response = connection.getresponse()
                    self.assertEqual(response.status, 400)
                    response.read()
                    request.assert_not_called()
            finally:
                connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
