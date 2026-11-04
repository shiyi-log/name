#!/usr/bin/env python3
"""使用发布数据库的独立副本检查本地 HTTP 应用。"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener


ROOT = Path(__file__).resolve().parent.parent


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="name-api-smoke-") as directory:
        workspace = Path(directory)
        for filename in (
            "api_server.py", "build_database.py", "scoring.py", "index.html",
            "app.js", "styles.css",
        ):
            shutil.copy2(ROOT / filename, workspace / filename)
        shutil.copytree(
            ROOT / "data", workspace / "data",
            ignore=shutil.ignore_patterns("names.db", "*-wal", "*-shm"),
        )
        # SQLite 备份包含已提交的 WAL 数据，且不会修改源数据库。
        source_uri = (ROOT / "data/names.db").resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(source_uri, uri=True)) as source:
            expected_count = source.execute("SELECT count(*) FROM name_candidates").fetchone()[0]
            expected_lengths = dict(source.execute(
                "SELECT length(name), count(*) FROM name_candidates GROUP BY length(name)"
            ))
            with closing(sqlite3.connect(workspace / "data/names.db")) as target:
                source.backup(target)
        expected_characters = len(json.loads((workspace / "data/characters-8105.json").read_text()))
        check(expected_count > 0, "Bundled candidate database is empty")

        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        base_url = f"http://127.0.0.1:{port}"
        environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith("OPENAI_") and key != "CC_SWITCH_PROVIDER_ID"
        }
        environment["CC_SWITCH_DB_PATH"] = str(workspace / "missing-cc-switch.db")
        environment["PYTHONUNBUFFERED"] = "1"
        opener = build_opener(ProxyHandler({}))

        def request(path: str, payload: dict | None = None) -> tuple[int, bytes]:
            body = json.dumps(payload).encode() if payload is not None else None
            headers = {"Content-Type": "application/json"} if body is not None else {}
            outgoing = Request(base_url + path, data=body, headers=headers)
            try:
                with opener.open(outgoing, timeout=5) as response:
                    return response.status, response.read()
            except HTTPError as error:
                return error.code, error.read()

        def get_json(path: str) -> dict:
            status, body = request(path)
            check(status == 200, f"{path} returned HTTP {status}")
            return json.loads(body)

        with (workspace / "server.log").open("w+") as log:
            process = subprocess.Popen(
                [sys.executable, "api_server.py", "--host", "127.0.0.1", "--port", str(port)],
                cwd=workspace, env=environment, stdout=log, stderr=subprocess.STDOUT,
            )
            try:
                deadline = time.monotonic() + 30
                while True:
                    if process.poll() is not None:
                        raise RuntimeError(f"API server exited with status {process.returncode}")
                    try:
                        stats = get_json("/api/stats")
                        break
                    except (URLError, TimeoutError):
                        if time.monotonic() >= deadline:
                            raise RuntimeError("API server did not start within 30 seconds")
                        time.sleep(0.1)

                status, homepage = request("/")
                check(status == 200 and b"app.js" in homepage, "Homepage is unavailable")
                check(stats["candidateCount"] == expected_count, "Candidate total differs from SQLite")
                check(stats["characterCount"] == expected_characters, "Character total differs from JSON")
                check(stats["lengthCounts"] == {str(key): value for key, value in expected_lengths.items()},
                      "Candidate length totals differ from SQLite")

                for length in (2, 3, 4):
                    candidates = get_json("/api/candidates?" + urlencode({"length": length, "limit": 5}))
                    check(candidates["total"] == expected_lengths.get(length, 0), f"Incorrect {length}-character total")
                    check(all(len(item["name"]) == length for item in candidates["items"]),
                          f"Length filter returned an invalid {length}-character name")
                for sort in ("score", "registration", "distinctiveness", "readability", "poetry", "name"):
                    candidates = get_json("/api/candidates?" + urlencode({"sort": sort, "limit": 5}))
                    check(candidates["total"] == expected_count and len(candidates["items"]) == min(5, expected_count),
                          f"Sort mode {sort} failed")
                    if sort != "name":
                        scores = [item["score"] if sort == "score" else item["scores"][sort]
                                  for item in candidates["items"]]
                        check(scores == sorted(scores, reverse=True), f"Sort order {sort} is incorrect")
                characters = get_json("/api/characters?limit=5")
                check(characters["total"] == expected_characters and len(characters["items"]) == 5,
                      "Character pagination failed")
                check(get_json("/api/ai/status")["configured"] is False,
                      "Smoke test unexpectedly loaded a real AI configuration")
                providers = get_json("/api/ai/providers")
                check(providers["items"] == [] and providers["selectedId"] == "",
                      "Smoke test unexpectedly loaded AI providers")
                status, _ = request("/api/generate/by-character", {
                    "character": "云", "lengths": [2], "count": 1, "providerId": "missing-smoke-provider",
                })
                check(status == 400, "Unknown provider should return HTTP 400 without a model request")
                print(f"API 冒烟检查通过：{expected_count:,} 条候选、{expected_characters:,} 个汉字、6 种排序；未调用模型服务。")
            except Exception:
                log.flush()
                log.seek(0)
                print(log.read(), file=sys.stderr)
                raise
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    main()
