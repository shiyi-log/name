#!/usr/bin/env python3
"""SQLite-backed local API and static-file server for the naming workspace."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import urllib.error
import urllib.request
from collections import Counter
from contextlib import closing
from pathlib import Path
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from build_database import (
    CHARACTERS_PATH,
    DB_PATH,
    ROOT,
    TOKENS_PATH,
    build_corpus_stats,
    build_database,
    excluded_names,
    load_existing_candidates,
    load_json,
    stable_candidate_id,
    tier_for,
)
from scoring import CorpusStats, pinyin_profile, score_name


WEIGHTS = {
    "brand": 28,
    "readability": 20,
    "poetry": 20,
    "distinctiveness": 18,
    "registration": 14,
}
SORT_COLUMNS = {
    "score": "total_score DESC, name ASC",
    "registration": "json_extract(scores_json, '$.registration') DESC, total_score DESC, name ASC",
    "distinctiveness": "json_extract(scores_json, '$.distinctiveness') DESC, total_score DESC, name ASC",
    "readability": "json_extract(scores_json, '$.readability') DESC, total_score DESC, name ASC",
    "poetry": "json_extract(scores_json, '$.poetry') DESC, total_score DESC, name ASC",
    "name": "name COLLATE NOCASE ASC",
}
CHINESE_RE = re.compile(r"^[\u4e00-\u9fff]+$")
PROVIDER_BASE = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
PROVIDER_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
CC_SWITCH_DB = Path(
    os.environ.get("CC_SWITCH_DB_PATH", str(Path.home() / ".cc-switch" / "cc-switch.db"))
)
_selected_provider_id: str | None = os.environ.get("CC_SWITCH_PROVIDER_ID") or None


def _toml_value(config: str, key: str) -> str:
    """Read the small scalar subset used by CC Switch provider configs."""
    match = re.search(rf"(?m)^\s*{re.escape(key)}\s*=\s*(['\"])(.*?)\1\s*$", config or "")
    return match.group(2).strip() if match else ""


def _provider_from_row(row: sqlite3.Row) -> dict | None:
    try:
        settings = json.loads(row["settings_config"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(settings, dict):
        return None
    auth = settings.get("auth") if isinstance(settings.get("auth"), dict) else {}
    api_key = ""
    for key in ("OPENAI_API_KEY", "api_key", "apiKey", "token"):
        value = auth.get(key)
        if isinstance(value, str) and value.strip():
            api_key = value.strip()
            break
    config = settings.get("config", "") if isinstance(settings.get("config"), str) else ""
    base_url = _toml_value(config, "base_url") or str(row["website_url"] or "").strip()
    if not base_url or not api_key:
        return None
    wire_api = (_toml_value(config, "wire_api") or "chat_completions").lower()
    if wire_api not in {"responses", "chat_completions", "chat-completions"}:
        wire_api = "chat_completions"
    catalog = settings.get("modelCatalog") if isinstance(settings.get("modelCatalog"), dict) else {}
    models = []
    for model in catalog.get("models", []) if isinstance(catalog.get("models"), list) else []:
        if isinstance(model, dict) and isinstance(model.get("model"), str) and model["model"].strip():
            models.append(model["model"].strip())
    default_model = _toml_value(config, "model") or (models[0] if models else "")
    if default_model and default_model not in models:
        models.insert(0, default_model)
    return {
        "id": str(row["id"]),
        "name": str(row["name"]),
        "appType": str(row["app_type"]),
        "baseUrl": base_url.rstrip("/"),
        "wireApi": wire_api.replace("-", "_"),
        "model": default_model,
        "models": models,
        "websiteUrl": str(row["website_url"] or ""),
        "current": bool(row["is_current"]),
        "sortIndex": int(row["sort_index"] or 0),
        "_api_key": api_key,
    }


def load_cc_switch_providers() -> list[dict]:
    """Import API providers from CC Switch without persisting their secrets.

    The official Codex account entry is OAuth-backed and is intentionally skipped;
    only providers carrying an OpenAI-compatible API key are exposed to the app.
    """
    if not CC_SWITCH_DB.exists():
        return []
    uri = f"file:{CC_SWITCH_DB}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """SELECT id, app_type, name, settings_config, website_url, sort_index, is_current
                   FROM providers
                   WHERE app_type IN ('codex', 'openai')
                   ORDER BY is_current DESC, COALESCE(sort_index, 999999), name"""
            ).fetchall()
    except (OSError, sqlite3.Error):
        return []
    providers = []
    seen = set()
    for row in rows:
        provider = _provider_from_row(row)
        if provider is None or provider["id"] in seen:
            continue
        seen.add(provider["id"])
        providers.append(provider)
    return providers


def _env_provider() -> dict | None:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None
    wire_api = os.environ.get("OPENAI_WIRE_API", "chat_completions").lower().replace("-", "_")
    if wire_api not in {"responses", "chat_completions"}:
        wire_api = "chat_completions"
    return {
        "id": "environment",
        "name": "环境变量",
        "appType": "openai",
        "baseUrl": os.environ.get("OPENAI_BASE_URL", PROVIDER_BASE).rstrip("/"),
        "wireApi": wire_api,
        "model": os.environ.get("OPENAI_MODEL", PROVIDER_MODEL),
        "models": [os.environ.get("OPENAI_MODEL", PROVIDER_MODEL)],
        "websiteUrl": "",
        "current": False,
        "sortIndex": -1,
        "_api_key": api_key,
    }


def available_ai_providers() -> list[dict]:
    providers = load_cc_switch_providers()
    env_provider = _env_provider()
    if env_provider:
        providers.insert(0, env_provider)
    return providers


def selected_ai_provider(provider_id: str | None = None) -> dict | None:
    providers = available_ai_providers()
    if provider_id is not None:
        return next((provider for provider in providers if provider["id"] == provider_id), None)
    requested = _selected_provider_id
    if requested:
        for provider in providers:
            if provider["id"] == requested:
                return provider
    return providers[0] if providers else None


def public_ai_provider(provider: dict) -> dict:
    result = {key: value for key, value in provider.items() if not key.startswith("_")}
    result["protocol"] = "Responses" if provider["wireApi"] == "responses" else "Chat Completions"
    return result


def row_to_candidate(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "length": row["name_length"],
        "leftToken": row["left_token"],
        "rightToken": row["right_token"],
        "template": row["template"],
        "direction": row["direction"],
        "pitch": row["pitch"],
        "source": row["source"],
        "sourceType": row["source_type"],
        "aiModel": row["ai_model"],
        "tags": json.loads(row["tags_json"]),
        "scores": json.loads(row["scores_json"]),
        "score": row["total_score"],
        "reasons": json.loads(row["reasons_json"]),
        "notes": json.loads(row["notes_json"]),
        "status": row["status"],
        "registryNote": row["registry_note"],
    }


def quantile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * ratio))]


def ensure_database() -> None:
    if DB_PATH.exists():
        with closing(sqlite3.connect(DB_PATH)) as connection:
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='name_candidates'"
            ).fetchone()
            if table:
                characters = load_json(CHARACTERS_PATH)
                tokens = load_json(TOKENS_PATH)
                build_database(characters, tokens, [])
                return
    build_database(load_json(CHARACTERS_PATH), load_json(TOKENS_PATH), [])


def api_stats() -> dict:
    with closing(sqlite3.connect(DB_PATH)) as connection:
        connection.row_factory = sqlite3.Row
        total = connection.execute("SELECT count(*) FROM name_candidates").fetchone()[0]
        characters = connection.execute("SELECT count(*) FROM characters").fetchone()[0]
        length_rows = connection.execute(
            "SELECT name_length, count(*) AS count FROM name_candidates GROUP BY name_length"
        ).fetchall()
        directions = [
            row[0] for row in connection.execute(
                "SELECT DISTINCT direction FROM name_candidates ORDER BY direction"
            )
        ]
        scores = [row[0] for row in connection.execute("SELECT total_score FROM name_candidates")]
    high_threshold = quantile(scores, 0.9)
    mid_threshold = quantile(scores, 0.65)
    return {
        "candidateCount": total,
        "characterCount": characters,
        "highScoreCount": sum(score >= high_threshold for score in scores),
        "highThreshold": round(high_threshold, 1),
        "midThreshold": round(mid_threshold, 1),
        "combinationSpace": str(6500**4),
        "lengthCounts": {str(row["name_length"]): row["count"] for row in length_rows},
        "directions": directions,
    }


def query_candidates(params: dict[str, list[str]]) -> dict:
    length = params.get("length", ["all"])[0]
    query = params.get("query", [""])[0].strip()
    direction = params.get("direction", ["all"])[0]
    initial = params.get("initial", [""])[0]
    character = params.get("character", [""])[0]
    sort = params.get("sort", ["score"])[0]
    minimum_score = max(0.0, min(100.0, float(params.get("minimum_score", ["0"])[0])))
    limit = max(1, min(100, int(params.get("limit", ["60"])[0])))
    offset = max(0, int(params.get("offset", ["0"])[0]))
    if length not in {"all", "2", "3", "4"}:
        raise ValueError("length must be all, 2, 3, or 4")
    if sort not in SORT_COLUMNS:
        raise ValueError("unsupported sort mode")

    where = ["total_score >= ?"]
    values: list[object] = [minimum_score]
    if length != "all":
        where.append("name_length = ?")
        values.append(int(length))
    if direction != "all":
        where.append("direction = ?")
        values.append(direction)
    if initial:
        where.append("substr(name, 1, 1) = ?")
        values.append(initial)
    if character:
        where.append("instr(name, ?) > 0")
        values.append(character)
    if query:
        where.append("(name LIKE ? OR pitch LIKE ? OR source LIKE ? OR direction LIKE ? OR tags_json LIKE ?)")
        values.extend([f"%{query}%"] * 5)
    predicate = " AND ".join(where)
    with closing(sqlite3.connect(DB_PATH)) as connection:
        connection.row_factory = sqlite3.Row
        total = connection.execute(
            f"SELECT count(*) FROM name_candidates WHERE {predicate}", values
        ).fetchone()[0]
        rows = connection.execute(
            f"""SELECT id, name, name_length, left_token, right_token, template, direction, pitch, source,
                       source_type, ai_model, tags_json, scores_json, total_score, reasons_json,
                       notes_json, status, registry_note
                FROM name_candidates WHERE {predicate}
                ORDER BY {SORT_COLUMNS[sort]} LIMIT ? OFFSET ?""",
            [*values, limit, offset],
        ).fetchall()
    return {"items": [row_to_candidate(row) for row in rows], "total": total}


def query_characters(params: dict[str, list[str]]) -> dict:
    query = params.get("query", [""])[0].strip()
    tier = params.get("tier", ["all"])[0]
    limit = max(1, min(240, int(params.get("limit", ["120"])[0])))
    offset = max(0, int(params.get("offset", ["0"])[0]))
    where = []
    values: list[object] = []
    if query:
        where.append("character LIKE ?")
        values.append(f"%{query}%")
    if tier != "all":
        where.append("tier = ?")
        values.append(int(tier))
    predicate = f"WHERE {' AND '.join(where)}" if where else ""
    with closing(sqlite3.connect(DB_PATH)) as connection:
        connection.row_factory = sqlite3.Row
        total = connection.execute(f"SELECT count(*) FROM characters {predicate}", values).fetchone()[0]
        rows = connection.execute(
            f"SELECT character, tier, rank FROM characters {predicate} ORDER BY rank LIMIT ? OFFSET ?",
            [*values, limit, offset],
        ).fetchall()
    return {"items": [dict(row) for row in rows], "total": total}


def ai_status() -> dict:
    provider = selected_ai_provider()
    return {
        "configured": provider is not None,
        "model": provider["model"] if provider else PROVIDER_MODEL,
        "providerId": provider["id"] if provider else "",
        "providerName": provider["name"] if provider else "",
        "wireApi": provider["wireApi"] if provider else "",
    }


def ai_providers() -> dict:
    providers = available_ai_providers()
    selected = selected_ai_provider()
    public = [public_ai_provider(provider) for provider in providers]
    return {
        "providers": public,
        "items": public,
        "selectedProviderId": selected["id"] if selected else "",
        "selectedId": selected["id"] if selected else "",
    }


def _response_endpoint(base_url: str, wire_api: str) -> str:
    suffix = "/responses" if wire_api == "responses" else "/chat/completions"
    return base_url.rstrip("/") + suffix


def _response_text(result: dict, wire_api: str) -> str:
    if wire_api == "responses":
        output_text = result.get("output_text")
        if isinstance(output_text, str) and output_text.strip():
            return output_text
        chunks = []
        for output in result.get("output", []) if isinstance(result.get("output"), list) else []:
            for content in output.get("content", []) if isinstance(output, dict) and isinstance(output.get("content"), list) else []:
                if isinstance(content, dict):
                    text = content.get("text")
                    if isinstance(text, str):
                        chunks.append(text)
        return "".join(chunks)
    message = result.get("choices", [{}])[0].get("message", {})
    content = message.get("content", "") if isinstance(message, dict) else ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "") for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return ""


def _json_from_ai_text(content: str) -> dict:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
        if not fenced:
            fenced = re.search(r"(\{.*\})", content, re.DOTALL)
        if not fenced:
            raise RuntimeError("AI 返回内容不是有效 JSON")
        try:
            payload = json.loads(fenced.group(1))
        except json.JSONDecodeError as error:
            raise RuntimeError("AI 返回内容不是有效 JSON") from error
    if not isinstance(payload, dict):
        raise RuntimeError("AI 返回 JSON 不是对象")
    return payload


def request_ai_names(character: str, count: int, lengths: list[int], provider_id: str | None = None) -> list[dict]:
    provider = selected_ai_provider(provider_id)
    if provider is None:
        raise RuntimeError("尚未从 CC Switch 导入可用 AI 服务商，也未配置 OPENAI_API_KEY")
    api_key = provider["_api_key"]
    wire_api = provider["wireApi"]
    endpoint = _response_endpoint(provider["baseUrl"], wire_api)
    system = (
        "你是中文品牌命名顾问。按要求生成简洁、可读、有语义的中文名称。"
        "只返回 JSON 对象，格式为 {\"names\":[{\"name\":\"\",\"meaning\":\"\"}]}，不要 Markdown。"
    )
    prompt = {
        "anchor_character": character,
        "allowed_lengths": lengths,
        "requested_count_per_length": count,
        "rules": [
            "每个名称必须包含 anchor_character",
            "只使用简体中文汉字，名称长度必须严格匹配 allowed_lengths 中一个长度",
            "优先使用现代、好读、有韵味且适合公司品牌的表达",
            "避开常见成语、明显生僻字、谐音歧义、负面含义和行业通用词",
            "每个名称给出一句简短含义",
        ],
    }
    model = os.environ.get("OPENAI_MODEL") or provider["model"] or PROVIDER_MODEL
    if wire_api == "responses":
        request_payload = {
            "model": model,
            "temperature": 0.9,
            "input": [
                {"role": "system", "content": [{"type": "input_text", "text": system}]},
                {"role": "user", "content": [{"type": "input_text", "text": json.dumps(prompt, ensure_ascii=False)}]},
            ],
            "text": {"format": {"type": "json_object"}},
        }
    else:
        request_payload = {
            "model": model,
            "temperature": 0.9,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ],
        }
    body = json.dumps(request_payload).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read(1000).decode("utf-8", errors="replace")
        raise RuntimeError(f"AI 服务返回 HTTP {error.code}: {detail}") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise RuntimeError(f"AI 服务连接失败：{error}") from error

    content = _response_text(result, wire_api)
    payload = _json_from_ai_text(content)
    names = payload.get("names", []) if isinstance(payload, dict) else []
    if not isinstance(names, list):
        raise RuntimeError("AI 返回 JSON 缺少 names 列表")
    return [item for item in names if isinstance(item, dict)]


def generate_for_character(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("请求必须是 JSON 对象")
    character = payload.get("character", "")
    count = payload.get("count", 8)
    lengths = payload.get("lengths", [2, 3, 4])
    if type(count) is not int:
        raise ValueError("每种长度的数量必须是整数")
    count = max(1, min(30, count))
    if not isinstance(lengths, list) or not lengths or any(
        type(length) is not int or length not in (2, 3, 4) for length in lengths
    ):
        raise ValueError("候选长度只能选择 2、3、4 字")
    lengths = sorted(set(lengths))
    provider_id = payload.get("providerId")
    if provider_id is not None and not isinstance(provider_id, str):
        raise ValueError("providerId 必须是字符串")
    if provider_id and selected_ai_provider(provider_id) is None:
        raise ValueError("所选 AI 服务商不可用，请刷新页面后重试")
    characters = load_json(CHARACTERS_PATH)
    if not isinstance(character, str) or len(character) != 1 or character not in characters:
        raise ValueError("请选择字库中的一个汉字")

    raw_names = (
        request_ai_names(character, count, lengths, provider_id)
        if provider_id
        else request_ai_names(character, count, lengths)
    )
    characters_set = set(characters)
    tier_map = {char: tier_for(index) for index, char in enumerate(characters)}
    excluded = excluded_names()
    existing = load_existing_candidates(DB_PATH)
    existing_by_name = {item["name"]: item for item in existing}
    accepted: dict[str, str] = {}
    for item in raw_names:
        name = str(item.get("name", "")).strip()
        meaning = str(item.get("meaning", "")).strip()[:300]
        if len(name) not in lengths or character not in name or not CHINESE_RE.fullmatch(name):
            continue
        if not set(name).issubset(characters_set) or max(tier_map[char] for char in name) > 2:
            continue
        if name in excluded or len(set(name)) < 2 or name in existing_by_name:
            continue
        accepted.setdefault(name, meaning or "由 AI 生成，待人工核对语义和品牌适配度。")

    combinations = [
        (item["name"], item["leftToken"], item["rightToken"], "ai", "ai")
        for item in existing
    ]
    stats = CorpusStats()
    stats.char_rank = {char: index for index, char in enumerate(characters)}
    stats.build(
        [item[0] for item in combinations],
        [item[1] for item in combinations],
        [item[2] for item in combinations],
    )
    common_chars = set(characters[:3500])
    provider = selected_ai_provider(provider_id)
    model = os.environ.get("OPENAI_MODEL") or (provider["model"] if provider else PROVIDER_MODEL)
    new_items = []
    for name, meaning in accepted.items():
        result = score_name(name, name[:2], name[2:], "ai", "ai", common_chars, stats, pinyin_profile(name))
        new_items.append({
            "id": stable_candidate_id(name),
            "name": name,
            "leftToken": name[:2],
            "rightToken": name[2:],
            "template": "ai-generated",
            "direction": "东方与质感",
            "pitch": meaning,
            "source": f"AI 生成 · {model}",
            "sourceType": "ai",
            "aiModel": model,
            "tags": ["AI 生成", f"{len(name)}字"],
            "scores": result.scores,
            "score": result.total,
            "reasons": result.reasons,
            "notes": result.notes,
            "status": "待核验",
            "registryNote": "AI 生成候选；需要人工核对语义，并自行完成工商、商标和域名检索。",
        })

    if new_items:
        token_map = load_json(TOKENS_PATH)
        try:
            build_database(
                characters,
                token_map,
                new_items,
                db_path=DB_PATH,
            )
        except TypeError as error:
            # Keep compatibility with a caller that injects the pre-DB-path
            # three-argument writer; the real builder always accepts db_path.
            if "unexpected keyword argument 'db_path'" not in str(error):
                raise
            build_database(characters, token_map, new_items)
    return {
        "items": new_items,
        "added": len(new_items),
        "model": model,
        "rejected": len(raw_names) - len(new_items),
    }


class NamingRequestHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        try:
            if parsed.path == "/api/candidates":
                self.send_json(200, query_candidates(params))
            elif parsed.path == "/api/characters":
                self.send_json(200, query_characters(params))
            elif parsed.path == "/api/stats":
                self.send_json(200, api_stats())
            elif parsed.path == "/api/ai/status":
                self.send_json(200, ai_status())
            elif parsed.path == "/api/ai/providers":
                self.send_json(200, ai_providers())
            else:
                super().do_GET()
        except (ValueError, sqlite3.Error) as error:
            self.send_json(400, {"error": str(error)})

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/generate/by-character":
            self.send_json(404, {"error": "not found"})
            return
        origin = self.headers.get("Origin")
        expected_origin = f"http://{self.headers.get('Host', '')}"
        if (origin is not None and origin.lower() != expected_origin.lower()) or self.headers.get("Sec-Fetch-Site") == "cross-site":
            self.send_json(403, {"error": "只允许从本地应用页面生成候选"})
            return
        if self.headers.get_content_type() != "application/json":
            self.send_json(415, {"error": "Content-Type must be application/json"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or self.headers.get("Transfer-Encoding"):
                raise ValueError("invalid Content-Length")
            if length > 16_384:
                self.send_json(413, {"error": "request too large"})
                return
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("请求必须是 JSON 对象")
            self.send_json(200, generate_for_character(payload))
        except ValueError as error:
            self.send_json(400, {"error": str(error)})
        except RuntimeError as error:
            self.send_json(503, {"error": str(error)})
        except (json.JSONDecodeError, TypeError) as error:
            self.send_json(400, {"error": f"invalid request: {error}"})
        except Exception as error:
            self.send_json(502, {"error": f"generation failed: {error}"})


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Run the SQLite-backed naming app locally.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4173)
    args = parser.parse_args()
    ensure_database()
    server = ThreadingHTTPServer((args.host, args.port), NamingRequestHandler)
    print(f"名字研究所已启动：http://{args.host}:{args.port}/")
    print(f"SQLite 数据库：{DB_PATH}")
    server.serve_forever()


if __name__ == "__main__":
    main()
