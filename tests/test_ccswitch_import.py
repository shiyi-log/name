import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import api_server


class CCSwitchImportTests(unittest.TestCase):
    def test_import_reads_api_providers_without_exposing_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cc-switch.db"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(
                    """CREATE TABLE providers (
                        id TEXT, app_type TEXT, name TEXT, settings_config TEXT,
                        website_url TEXT, sort_index INTEGER, is_current INTEGER
                    )"""
                )
                connection.executemany(
                    "INSERT INTO providers VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            "oauth",
                            "codex",
                            "官方登录",
                            json.dumps({"auth": {"auth_mode": "chatgpt"}, "config": "model = \\\"gpt-6-sol\\\""}),
                            "",
                            0,
                            1,
                        ),
                        (
                            "api-provider",
                            "codex",
                            "测试 Responses",
                            json.dumps(
                                {
                                    "auth": {"OPENAI_API_KEY": "test-secret"},
                                    "config": (
                                        'model = "test-model"\n'
                                        'wire_api = "responses"\n'
                                        'base_url = "http://127.0.0.1:9000/v1"'
                                    ),
                                    "modelCatalog": {"models": [{"model": "test-model"}]},
                                }
                            ),
                            "https://example.invalid",
                            1,
                            0,
                        ),
                        (
                            "claude-provider",
                            "claude",
                            "不应导入",
                            json.dumps({"auth": {"OPENAI_API_KEY": "other-secret"}, "config": ""}),
                            "https://example.invalid",
                            2,
                            0,
                        ),
                    ],
                )
                connection.commit()

            with patch.object(api_server, "CC_SWITCH_DB", path), patch.object(
                api_server, "AI_CONFIG_PATH", Path(directory) / "ai-providers.json"
            ), patch.dict(
                api_server.os.environ, {"OPENAI_API_KEY": ""}, clear=False
            ):
                providers = api_server.available_ai_providers()
                payload = api_server.ai_providers()

            self.assertEqual([provider["name"] for provider in providers], ["测试 Responses"])
            self.assertEqual(providers[0]["wireApi"], "responses")
            self.assertEqual(providers[0]["baseUrl"], "http://127.0.0.1:9000/v1")
            self.assertNotIn("_api_key", payload["items"][0])
            self.assertNotIn("test-secret", json.dumps(payload, ensure_ascii=False))
            self.assertEqual(payload["selectedId"], "api-provider")

    def test_responses_provider_uses_responses_endpoint_and_auth_header(self):
        provider = {
            "id": "api-provider",
            "name": "测试 Responses",
            "baseUrl": "http://127.0.0.1:9000/v1",
            "wireApi": "responses",
            "model": "test-model",
            "_api_key": "test-secret",
        }
        response_body = json.dumps({
            "output_text": json.dumps({"names": [{"name": "云山", "meaning": "云与山"}]})
        }).encode("utf-8")
        with patch.object(api_server, "selected_ai_provider", return_value=provider), patch.object(
            api_server.urllib.request, "urlopen"
        ) as urlopen:
            response = urlopen.return_value.__enter__.return_value
            response.read.return_value = response_body

            names = api_server.request_ai_names("云", 3, [2])

        request = urlopen.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(names, [{"name": "云山", "meaning": "云与山"}])
        self.assertEqual(request.full_url, "http://127.0.0.1:9000/v1/responses")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-secret")
        self.assertEqual(payload["model"], "test-model")
        self.assertIn("input", payload)
        self.assertNotIn("messages", payload)


if __name__ == "__main__":
    unittest.main()
