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
        self.contexts.enter_context(patch.object(api_server, "AI_CONFIG_PATH", Path(directory) / "ai-providers.json"))
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

    def test_saved_local_provider_is_selected_without_exposing_its_api_key(self):
        config_path = self.provider_db.parent / "ai-config.json"
        with patch.object(api_server, "AI_CONFIG_PATH", config_path):
            result = api_server.save_ai_provider({
                "name": "Local Gateway",
                "baseUrl": "https://gateway.example/v1",
                "apiKey": "private-test-secret",
                "wireApi": "chat_completions",
                "model": "model-a",
            })
            provider_id = result["selectedId"]
            self.assertEqual(len(result["items"]), 1)
            self.assertEqual(result["items"][0]["model"], "model-a")
            self.assertNotIn("private-test-secret", json.dumps(result))
            self.assertEqual(api_server.selected_ai_provider()["_api_key"], "private-test-secret")
            self.assertEqual(api_server.selected_ai_provider()["id"], provider_id)
            self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)

    def test_saving_multiple_providers_preserves_existing_keys_and_model_catalog(self):
        config_path = self.provider_db.parent / "ai-config.json"
        with patch.object(api_server, "AI_CONFIG_PATH", config_path):
            first = api_server.save_ai_provider({
                "id": "first", "name": "First", "baseUrl": "https://one.example/v1",
                "apiKey": "first-secret", "wireApi": "chat_completions",
                "model": "one", "models": ["one", "two"],
            })
            second = api_server.save_ai_provider({
                "id": "second", "name": "Second", "baseUrl": "https://two.example/v1",
                "apiKey": "second-secret", "wireApi": "responses", "model": "two",
            })
            edited = api_server.save_ai_provider({
                "id": "first", "name": "First edited", "baseUrl": "https://one.example/v1",
                "apiKey": "", "wireApi": "chat_completions", "model": "two",
            })
            self.assertEqual(len(first["items"]), 1)
            self.assertEqual(second["selectedId"], "second")
            self.assertEqual(len(edited["items"]), 2)
            self.assertEqual(api_server.selected_ai_provider("first")["_api_key"], "first-secret")
            self.assertEqual(api_server.selected_ai_provider("first")["models"], ["one", "two"])
            api_server.select_ai_provider("first")
            self.assertEqual(api_server.selected_ai_provider()["id"], "first")

    def test_model_refresh_and_connectivity_check_use_saved_provider_without_returning_key(self):
        config_path = self.provider_db.parent / "ai-config.json"
        with patch.object(api_server, "AI_CONFIG_PATH", config_path):
            api_server.save_ai_provider({
                "id": "provider", "name": "Gateway", "baseUrl": "https://gateway.example/v1",
                "apiKey": "never-return-this", "wireApi": "chat_completions", "model": "old",
            })
            response = {"data": [{"id": "new-model"}, {"id": "other-model"}]}
            with patch.object(api_server.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(response).encode())) as request:
                models = api_server.refresh_ai_models({"providerId": "provider"})
            self.assertEqual(models["models"], ["new-model", "other-model"])
            self.assertEqual(request.call_args.args[0].full_url, "https://gateway.example/v1/models")
            with patch.object(api_server.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(response).encode())):
                checked = api_server.test_ai_provider({"providerId": "provider"})
            self.assertTrue(checked["ok"])
            self.assertNotIn("never-return-this", json.dumps(checked))
            self.assertEqual(api_server.selected_ai_provider("provider")["models"], ["new-model", "other-model"])

    def test_saving_provider_rejects_credentials_embedded_in_base_url(self):
        config_path = self.provider_db.parent / "ai-config.json"
        with patch.object(api_server, "AI_CONFIG_PATH", config_path):
            with self.assertRaisesRegex(ValueError, "有效的 HTTP 或 HTTPS 基础地址"):
                api_server.save_ai_provider({
                    "name": "Gateway",
                    "baseUrl": "https://user:password@gateway.example/v1",
                    "apiKey": "test-token",
                    "model": "model-a",
                })
        self.assertFalse(config_path.exists())

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

    def test_character_prompt_sets_per_length_counts_quality_and_deduplication(self):
        self.create_providers()
        result = {"choices": [{"message": {"content": json.dumps({"names": []})}}]}
        with patch.object(
            api_server.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(result).encode())
        ) as request:
            api_server.request_ai_names(
                "云", 2, [2, 3], "chat", requested_counts={2: 1, 3: 1}, avoid_names=["云山"]
            )

        body = json.loads(request.call_args.args[0].data)
        prompt = json.loads(body["messages"][1]["content"])
        self.assertEqual(prompt["requested_counts_per_length"], {"2": 1, "3": 1})
        self.assertEqual(prompt["avoid_existing_names"], ["云山"])
        self.assertEqual(prompt["dictionary_definition_context"]["definition"], "say, speak; clouds")
        self.assertEqual(prompt["dictionary_definition_context"]["version"], "17.0.0")
        rules = " ".join(prompt["rules"])
        self.assertIn("公司品牌", rules)
        self.assertIn("严格", rules)
        self.assertIn("重复", rules)
        self.assertIn("不假设特定行业", prompt["brand_brief"])
        self.assertIn("语义", prompt["diversity_guidance"])
        self.assertTrue(any("硬塞" in rule for rule in prompt["rules"]))
        self.assertIn("模板", prompt["diversity_guidance"])

    def test_selected_provider_model_is_saved_with_generated_candidates(self):
        self.create_providers()
        database_path = self.provider_db.parent / "names.db"
        characters = list("云山月")
        tokens = {"actions": [], "images": [], "abstracts": []}
        api_server.build_database(characters, tokens, [], database_path)
        with patch.object(api_server, "DB_PATH", database_path), patch.object(
            api_server, "load_json", side_effect=[characters, tokens]
        ), patch.object(api_server, "excluded_names", return_value=set()), patch.object(
            api_server, "request_ai_names", return_value=[{
                "name": "云山", "meaning": "云与山",
                "character_roles": {
                    "云": "云气构成自然意象的一部分",
                    "山": "山岳构成自然意象的一部分",
                },
                "naturalness": 5,
            }]
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
