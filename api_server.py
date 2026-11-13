#!/usr/bin/env python3
"""为取名工作区提供基于 SQLite 的本地接口和静态文件服务。"""

from __future__ import annotations

import json
import functools
import os
import re
import tempfile
import sqlite3
import uuid
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
LITERATURE_INDEX_PATH = ROOT / "参考文献" / "书目索引.json"
CHARACTER_DEFINITIONS_PATH = ROOT / "data" / "character_definitions.json"
PREFERRED_REFERENCE_BOOKS = ("说文解字", "尔雅", "小尔雅", "诗经", "楚辞", "周易")
VERIFIED_CHARACTER_MEANINGS = {
    "𬜯": {
        "meaning": "𬜯是一种草名；不得解释为目光、视见或眼睛。",
        "source": "https://www.unicode.org/Public/17.0.0/ucd/Unihan.zip",
        "rejected_role_terms": ("目光", "视见", "眼睛", "视野"),
    },
    "𫇭": {
        "meaning": "𫇭是蔿的简化字，可指芡的茎，也用作姓氏；不得误释为芦苇。",
        "source": "https://www.zdic.net/hans/蔿",
        "rejected_role_terms": ("芦苇",),
    },
}


@functools.lru_cache(maxsize=1)
def _character_definitions() -> dict:
    if not CHARACTER_DEFINITIONS_PATH.exists():
        return {}
    return json.loads(CHARACTER_DEFINITIONS_PATH.read_text(encoding="utf-8"))


def character_definition_context(character: str) -> dict | None:
    data = _character_definitions()
    entry = data.get("characters", {}).get(character)
    if not entry:
        return None
    return {**entry, "source_url": data.get("source_url"), "version": data.get("version")}


def is_character_name(name: str, characters: set[str]) -> bool:
    return bool(name) and all(character in characters for character in name)


def has_quality_evidence(item: dict, name: str | None = None) -> bool:
    roles = item.get("character_roles")
    naturalness = item.get("naturalness")
    if not isinstance(roles, dict):
        return False
    required = set(name) if name else set(roles)
    if not required or not required.issubset(roles):
        return False
    for character in required:
        role = roles[character]
        if not isinstance(role, str) or len(role.strip()) < 6:
            return False
        if any(term in role for term in ("前缀", "装饰", "硬加", "凑字")):
            return False
        # Identification alone supplies no word meaning or semantic relation.
        if any(term in role for term in ("核心识别字", "姓氏式识别字")):
            return False
        if any(term in role for term in ("替代同音字", "替换同音字")):
            return False
        if re.search(r'(?<!不)(?<!未)(?:形成|产生|带来)[^，。；]{0,24}(?:同音|音韵)联想', role):
            return False
        # “然” has an actual grammatical role in state words such as 蘧然.
        # A brand filler suffix remains insufficient semantic evidence.
        suffix_description = role.replace("状态后缀", "") if character == "然" else role
        if "后缀" in suffix_description:
            return False
        known = VERIFIED_CHARACTER_MEANINGS.get(character)
        if known and any(term in role for term in known['rejected_role_terms']):
            return False
        definition = character_definition_context(character)
        if definition and character == 'U0002cff5' and any(term in role for term in ('目光', '视见', '眼睛', '视野')):
            return False
    return type(naturalness) in (int, float) and 4 <= naturalness <= 5


@functools.lru_cache(maxsize=1)
def _reference_books() -> tuple[tuple[str, Path], ...]:
    if not LITERATURE_INDEX_PATH.exists():
        return ()
    books = json.loads(LITERATURE_INDEX_PATH.read_text(encoding="utf-8"))
    by_title = {book["title"]: book for book in books}
    result = []
    for title in PREFERRED_REFERENCE_BOOKS:
        book = by_title.get(title)
        if not book:
            continue
        relative = book.get("simplified_path") or book["text_path"]
        path = LITERATURE_INDEX_PATH.parent / relative
        if path.exists():
            result.append((title, path))
    return tuple(result)


def reference_contexts_for_character(
    character: str, corpus: dict[str, str] | None = None, max_per_book: int = 1,
) -> list[dict[str, str]]:
    if not isinstance(character, str) or len(character) != 1 or max_per_book < 1:
        return []
    contexts = []
    sources = corpus.items() if corpus is not None else _reference_books()
    for book, source in sources:
        text = source if isinstance(source, str) else source.read_text(encoding="utf-8")
        found = 0
        for line_number, line in enumerate(text.splitlines(), start=1):
            for sentence in re.split(r"(?<=[。！？；])", line):
                position = sentence.find(character)
                if position < 0:
                    continue
                start = max(0, position - 45)
                end = min(len(sentence), position + 65)
                context = sentence[start:end].strip()
                contexts.append({"book": book, "context": context, "line": str(line_number)})
                found += 1
                if found >= max_per_book:
                    break
            if found >= max_per_book:
                break
        if len(contexts) >= 6:
            break
    return contexts


def character_frequency_guidance(character: str) -> str:
    return (
        f"锚定字“{character}”是本次命名核心，即使字频低或字形生僻也必须自然参与语义；"
        "其他配字优先使用常见、易读且语义自然的汉字。若无法形成自然名称，应少交，不得硬凑。"
    )


MAX_PROMPT_AVOID_NAMES = 120


def prompt_avoid_names(names: list[str], limit: int = MAX_PROMPT_AVOID_NAMES) -> list[str]:
    """Keep a representative bounded sample in the model prompt."""
    unique = list(dict.fromkeys(names))
    if limit <= 0:
        return []
    if len(unique) <= limit:
        return unique
    last_index = len(unique) - 1
    return [unique[round(index * last_index / (limit - 1))] for index in range(limit)]


PROVIDER_BASE = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
PROVIDER_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
CC_SWITCH_DB = Path(
    os.environ.get("CC_SWITCH_DB_PATH", str(Path.home() / ".cc-switch" / "cc-switch.db"))
)
AI_CONFIG_PATH = Path(os.environ.get(
    "NAME_AI_CONFIG_PATH", str(Path.home() / ".config" / "name" / "ai-providers.json")
))
_selected_provider_id: str | None = os.environ.get("CC_SWITCH_PROVIDER_ID") or None


def _toml_value(config: str, key: str) -> str:
    """读取 CC Switch 服务商配置所用的简单标量字段。"""
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
    base_url = _toml_value(config, "base_url")
    parsed_url = urlparse(base_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc or not api_key:
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
    """从 CC Switch 导入接口服务商，但不持久化保存密钥。

    官方 Codex 账号使用 OAuth 授权，此处跳过；
    应用仅显示带有 OpenAI 兼容接口密钥的服务商。
    """
    if not CC_SWITCH_DB.exists():
        return []
    uri = CC_SWITCH_DB.resolve().as_uri() + "?mode=ro"
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
        try:
            provider = _provider_from_row(row)
        except (TypeError, ValueError):
            continue
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
    providers = load_saved_ai_providers()
    known_ids = {provider["id"] for provider in providers}
    providers.extend(provider for provider in load_cc_switch_providers() if provider["id"] not in known_ids)
    known_ids = {provider["id"] for provider in providers}
    env_provider = _env_provider()
    if env_provider and env_provider["id"] not in known_ids:
        providers.insert(0, env_provider)
    return providers


def selected_ai_provider(provider_id: str | None = None) -> dict | None:
    providers = available_ai_providers()
    configured_id = _read_ai_config()["selectedId"]
    requested = provider_id or configured_id or _selected_provider_id
    if requested:
        for provider in providers:
            if provider["id"] == requested:
                return provider
        if provider_id:
            raise ValueError("所选 AI 服务商不可用，请刷新配置后重试")
        return None
    return providers[0] if providers else None


def public_ai_provider(provider: dict) -> dict:
    return {key: provider[key] for key in ("id", "name", "model", "models", "wireApi", "current")}


def _read_ai_config() -> dict:
    try:
        config = json.loads(AI_CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"selectedId": "", "providers": []}
    except (OSError, json.JSONDecodeError):
        return {"selectedId": "", "providers": []}
    if not isinstance(config, dict) or not isinstance(config.get("providers"), list):
        return {"selectedId": "", "providers": []}
    return {
        "selectedId": str(config.get("selectedId", "")),
        "providers": [item for item in config["providers"] if isinstance(item, dict)],
    }


def _write_ai_config(config: dict) -> None:
    AI_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        AI_CONFIG_PATH.parent.chmod(0o700)
    except OSError:
        pass
    descriptor, temporary_path = tempfile.mkstemp(prefix=".ai-providers-", dir=AI_CONFIG_PATH.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            json.dump(config, target, ensure_ascii=False, indent=2)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary_path, AI_CONFIG_PATH)
        AI_CONFIG_PATH.chmod(0o600)
    except Exception:
        try:
            os.unlink(temporary_path)
        except OSError:
            pass
        raise


def load_saved_ai_providers() -> list[dict]:
    providers = []
    for item in _read_ai_config()["providers"]:
        provider_id = item.get("id")
        base_url = item.get("baseUrl")
        api_key = item.get("apiKey")
        wire_api = item.get("wireApi", "chat_completions")
        if not all(isinstance(value, str) and value.strip() for value in (provider_id, base_url, api_key)):
            continue
        parsed_url = urlparse(base_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            continue
        if wire_api not in {"responses", "chat_completions"}:
            wire_api = "chat_completions"
        models = item.get("models") if isinstance(item.get("models"), list) else []
        models = list(dict.fromkeys(
            model.strip() for model in models if isinstance(model, str) and model.strip()
        ))[:500]
        model = item.get("model") if isinstance(item.get("model"), str) else ""
        if model and model not in models:
            models.insert(0, model)
        providers.append({
            "id": provider_id,
            "name": str(item.get("name") or "OpenAI 兼容服务商"),
            "appType": "openai",
            "baseUrl": base_url.rstrip("/"),
            "wireApi": wire_api,
            "model": model,
            "models": models,
            "current": False,
            "_api_key": api_key,
        })
    return providers


def _management_provider(payload: dict) -> dict:
    provider_id = payload.get("providerId") or payload.get("id") or ""
    saved = next((item for item in load_saved_ai_providers() if item["id"] == provider_id), None)
    if saved is None:
        saved = next((item for item in [*load_cc_switch_providers(), *([_env_provider()] if _env_provider() else [])]
                      if item.get("id") == provider_id), None)
    base_url = payload.get("baseUrl", saved["baseUrl"] if saved else "")
    api_key = payload.get("apiKey", "")
    api_key = api_key.strip() if isinstance(api_key, str) else ""
    if not api_key and saved:
        api_key = saved["_api_key"]
    wire_api = payload.get("wireApi", saved["wireApi"] if saved else "chat_completions")
    if not isinstance(base_url, str) or len(base_url) > 2048:
        raise ValueError("API 地址无效")
    base_url = base_url.strip().rstrip("/")
    parsed_url = urlparse(base_url)
    if (parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc
        or parsed_url.query or parsed_url.fragment or parsed_url.username or parsed_url.password):
        raise ValueError("API 地址必须是有效的 HTTP 或 HTTPS 基础地址")
    if wire_api not in {"responses", "chat_completions"}:
        raise ValueError("请选择 Responses 或 Chat Completions 协议")
    if not api_key:
        raise ValueError("请先填写 API Key 并保存，或在此表单中输入 API Key")
    return {
        "id": provider_id,
        "name": str(payload.get("name") or (saved["name"] if saved else "临时配置")),
        "baseUrl": base_url,
        "wireApi": wire_api,
        "model": str(payload.get("model") or (saved["model"] if saved else "")),
        "models": saved["models"] if saved else [],
        "_api_key": api_key,
    }


def save_ai_provider(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("配置必须是 JSON 对象")
    config = _read_ai_config()
    provider_id = payload.get("id") or str(uuid.uuid4())
    if not isinstance(provider_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", provider_id):
        raise ValueError("服务商 ID 格式无效")
    name = payload.get("name", "")
    base_url = payload.get("baseUrl", "")
    api_key = payload.get("apiKey", "")
    wire_api = payload.get("wireApi", "chat_completions")
    model = payload.get("model", "")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100:
        raise ValueError("请填写 1 到 100 个字符的服务商名称")
    if not isinstance(base_url, str) or len(base_url) > 2048:
        raise ValueError("API 地址无效")
    base_url = base_url.strip().rstrip("/")
    parsed_url = urlparse(base_url)
    if (parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc
        or parsed_url.query or parsed_url.fragment or parsed_url.username or parsed_url.password):
        raise ValueError("API 地址必须是有效的 HTTP 或 HTTPS 基础地址，且不含查询参数")
    if wire_api not in {"responses", "chat_completions"}:
        raise ValueError("请选择 Responses 或 Chat Completions 协议")
    if not isinstance(model, str) or len(model) > 200:
        raise ValueError("模型名称无效")
    if not model.strip():
        raise ValueError("请先刷新并选择模型，或手动填写模型 ID")
    if not isinstance(api_key, str) or len(api_key) > 4096:
        raise ValueError("API Key 无效")
    previous = next((item for item in config["providers"] if item.get("id") == provider_id), None)
    if previous is None:
        previous = next(
            (item for item in [*load_cc_switch_providers(), *([_env_provider()] if _env_provider() else [])]
             if item.get("id") == provider_id),
            {},
        )
    api_key = api_key.strip() or str(previous.get("apiKey", "")).strip()
    if not api_key:
        api_key = str(previous.get("_api_key", "")).strip()
    if not api_key:
        raise ValueError("请填写 API Key")
    models = payload.get("models", previous.get("models", []))
    if not isinstance(models, list):
        raise ValueError("模型列表格式无效")
    models = list(dict.fromkeys(
        value.strip() for value in models if isinstance(value, str) and value.strip()
    ))[:500]
    if model.strip() and model.strip() not in models:
        models.insert(0, model.strip())
    provider = {
        "id": provider_id,
        "name": name.strip(),
        "baseUrl": base_url,
        "apiKey": api_key,
        "wireApi": wire_api,
        "model": model.strip(),
        "models": models,
    }
    updated = False
    for index, item in enumerate(config["providers"]):
        if item.get("id") == provider_id:
            config["providers"][index] = provider
            updated = True
            break
    if not updated:
        config["providers"].append(provider)
    config["selectedId"] = provider_id
    _write_ai_config(config)
    return ai_config()


def select_ai_provider(provider_id: str) -> dict:
    if not isinstance(provider_id, str) or not provider_id.strip():
        raise ValueError("请选择一个 AI 服务商")
    provider = next((item for item in available_ai_providers() if item["id"] == provider_id), None)
    if provider is None:
        raise ValueError("所选 AI 服务商不可用")
    config = _read_ai_config()
    config["selectedId"] = provider_id
    _write_ai_config(config)
    global _selected_provider_id
    _selected_provider_id = provider_id
    return ai_config()


def _provider_request(provider: dict, suffix: str, *, method: str = "GET", body: dict | None = None) -> dict:
    url = provider["baseUrl"].rstrip("/") + suffix
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        url, data=data, headers={"Authorization": f"Bearer {provider['_api_key']}",
                               "Content-Type": "application/json"}, method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"AI 服务返回 HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise RuntimeError(f"连接 AI 服务失败：{error}") from error
    if not isinstance(result, dict):
        raise RuntimeError("AI 服务返回内容格式无效")
    return result


def _extract_models(payload: dict) -> list[str]:
    entries = payload.get("data", payload.get("models", []))
    if isinstance(entries, dict):
        entries = entries.get("data", [])
    if not isinstance(entries, list):
        raise RuntimeError("模型接口没有返回模型列表")
    models = []
    for entry in entries:
        model = entry.get("id") or entry.get("name") or entry.get("model") if isinstance(entry, dict) else entry
        if isinstance(model, str) and model.strip():
            models.append(model.strip())
    return list(dict.fromkeys(models))[:500]


def refresh_ai_models(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("服务商配置无效")
    provider = _management_provider(payload)
    provider_id = provider["id"]
    models = _extract_models(_provider_request(provider, "/models"))
    config = _read_ai_config()
    for item in config["providers"]:
        if item.get("id") == provider_id:
            item["models"] = models
            if item.get("model") not in models:
                item["model"] = models[0] if models else ""
            _write_ai_config(config)
            break
    selected_model = provider["model"] if provider["model"] in models else (models[0] if models else "")
    return {"providerId": provider_id, "model": selected_model, "models": models}


def test_ai_provider(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("服务商配置无效")
    provider = _management_provider(payload)
    provider_id = provider["id"]
    result = _provider_request(provider, "/models")
    models = _extract_models(result)
    return {"ok": True, "providerId": provider_id, "modelCount": len(models), "message": "连接成功"}


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
    cursor_value = params.get("after", [""])[0]
    cursor = None
    if cursor_value:
        try:
            cursor = json.loads(cursor_value)
        except json.JSONDecodeError as error:
            raise ValueError("invalid candidate cursor") from error
        if not isinstance(cursor, dict) or not isinstance(cursor.get("name"), str):
            raise ValueError("invalid candidate cursor")
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
    base_predicate = " AND ".join(where)
    base_values = list(values)
    if cursor:
        name = cursor["name"]
        if sort == "name":
            where.append("name COLLATE NOCASE > ?")
            values.append(name)
        else:
            metric = sort if sort in {"registration", "distinctiveness", "readability", "poetry"} else None
            score = float(cursor.get("score", 0))
            if metric:
                cursor_metric = float((cursor.get("scores") or {}).get(metric, 0))
                where.append(
                    f"(json_extract(scores_json, '$.{metric}') < ? OR "
                    f"(json_extract(scores_json, '$.{metric}') = ? AND "
                    "(total_score < ? OR (total_score = ? AND name > ?)))"
                )
                values.extend([cursor_metric, cursor_metric, score, score, name])
            else:
                where.append("(total_score < ? OR (total_score = ? AND name > ?))")
                values.extend([score, score, name])
    predicate = " AND ".join(where)
    with closing(sqlite3.connect(DB_PATH)) as connection:
        connection.row_factory = sqlite3.Row
        total = connection.execute(
            f"SELECT count(*) FROM name_candidates WHERE {base_predicate}", base_values
        ).fetchone()[0]
        query_limit = limit + 1 if cursor else limit
        rows = connection.execute(
            f"""SELECT id, name, name_length, left_token, right_token, template, direction, pitch, source,
                       source_type, ai_model, tags_json, scores_json, total_score, reasons_json,
                       notes_json, status, registry_note
                FROM name_candidates WHERE {predicate}
                ORDER BY {SORT_COLUMNS[sort]} LIMIT ? OFFSET ?""",
            [*values, query_limit, 0 if cursor else offset],
        ).fetchall()
    has_more = len(rows) > limit if cursor else offset + len(rows) < total
    return {"items": [row_to_candidate(row) for row in rows[:limit]], "total": total, "hasMore": has_more}


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
    return {
        "items": [public_ai_provider(provider) for provider in providers],
        "selectedId": selected["id"] if selected else "",
    }


def ai_config() -> dict:
    config = _read_ai_config()
    saved_ids = {item["id"] for item in load_saved_ai_providers()}
    providers = available_ai_providers()
    selected = selected_ai_provider()
    return {
        "items": [
            {
                key: provider[key]
                for key in ("id", "name", "baseUrl", "wireApi", "model", "models")
            } | {"apiKeyConfigured": bool(provider.get("_api_key"))}
              | {"managed": provider["id"] in saved_ids}
            for provider in providers
        ],
        "selectedId": selected["id"] if selected else config["selectedId"],
    }


def _response_endpoint(base_url: str, wire_api: str) -> str:
    suffix = "/responses" if wire_api == "responses" else "/chat/completions"
    return base_url.rstrip("/") + suffix


def _response_text(result: dict, wire_api: str) -> str:
    if not isinstance(result, dict):
        raise RuntimeError("AI 服务返回内容不是 JSON 对象")
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
    choices = result.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise RuntimeError("AI 服务返回内容缺少有效 choices")
    message = choices[0].get("message", {})
    content = message.get("content", "") if isinstance(message, dict) else ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "") for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return ""


def _read_responses_stream(response) -> dict:
    data_lines = []
    completed = None
    for raw_line in response:
        line = raw_line.decode("utf-8").strip() if isinstance(raw_line, bytes) else raw_line.strip()
        if not line:
            if data_lines:
                event = json.loads("\n".join(data_lines))
                data_lines.clear()
                if event.get("type") == "response.completed":
                    completed = event.get("response")
                    if not isinstance(completed, dict):
                        raise RuntimeError("AI Responses 流未返回完成事件")
                    return completed
                elif event.get("type") == "response.failed":
                    raise RuntimeError("AI Responses 流式请求失败")
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        event = json.loads("\n".join(data_lines))
        if event.get("type") == "response.completed":
            completed = event.get("response")
        elif event.get("type") == "response.failed":
            raise RuntimeError("AI Responses 流式请求失败")
    if not isinstance(completed, dict):
        raise RuntimeError("AI Responses 流未返回完成事件")
    return completed


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


def request_ai_names(
    character: str, count: int, lengths: list[int], provider_id: str | None = None,
    *, provider: dict | None = None, requested_counts: dict[int, int] | None = None,
    avoid_names: list[str] | None = None,
    review_feedback: list[dict] | None = None,
) -> list[dict]:
    provider = provider if provider is not None else selected_ai_provider(provider_id)
    if provider is None:
        raise RuntimeError("尚未从 CC Switch 导入可用 AI 服务商，也未配置 OPENAI_API_KEY")
    api_key = provider["_api_key"]
    wire_api = provider["wireApi"]
    endpoint = _response_endpoint(provider["baseUrl"], wire_api)
    system = (
        "你是资深中文品牌命名顾问，为真实企业构思自然、鲜明、可传播的中文名称。"
        "先理解锚定字可承载的常见语义、联想与读音，再围绕不同的品牌意象发散构思；"
        "每个名字都要读起来像自然成立的中文名称，而不是为了塞入锚定字而拼出的字串。"
        "逐个检查字符、长度、读音、语义、重复、近似度和品牌适配度，只输出通过检查的结果。"
        "不同名称必须真正有区别；不能为了凑数输出换序、改一字、同义替换的近似名或机械拼接。锚字不能总放在开头。"
        "只返回 JSON 对象，格式为 {\"names\":[{\"name\":\"\",\"meaning\":\"\",\"character_roles\":{\"字\":\"该字在名称中的具体语义作用\"},\"naturalness\":5}]}，不要 Markdown 或额外字段。"
    )
    prompt = {
        "anchor_character": character,
        "brand_brief": (
            "为中文公司构思可长期使用的品牌名；行业、产品、地域和创始人背景均未知，"
            "不假设特定行业，也不把名称局限在单一品类。"
        ),
        "allowed_lengths": lengths,
        "requested_count_per_length": count,
        "requested_counts_per_length": {str(length): requested_counts[length] for length in lengths}
        if requested_counts is not None else {str(length): count for length in lengths},
        "avoid_existing_names": prompt_avoid_names(avoid_names or []),
        "quality_review_feedback": (review_feedback or [])[:20],
        "diversity_guidance": (
            "在满足锚定字、长度和质量约束的前提下，让名称在整体意象、语义方向、"
            "用字组合和语感上彼此有辨识度；可自然探索山水、光影、时序、品格、"
            "创造、连接、远行等不同语义方向，不要把某个方向或固定字模板重复铺满。"
            "锚字在名称中的位置要变化，不要总用“锚字+通用词”模式；优先构思自然存在的词语、典故意象或有清楚语义关系的组合。"
        ),
        "literature_reference": (
            "本项目参考库整理了《诗经》《楚辞》《周易》《论语》《庄子》《道德经》、"
            "全唐诗、全宋词及神话志怪等88种资料。仅将其作为字义、意象和命名灵感参考；"
            "有贴切联想时优先从这些传统意象发散，没有贴切内容时用自然现代中文命名，"
            "不可伪造原句、出处或把普通组合说成古籍原词。"
        ),
        "literature_context_examples": reference_contexts_for_character(character),
        "verified_character_meaning": VERIFIED_CHARACTER_MEANINGS.get(character),
        "dictionary_definition_context": character_definition_context(character),
        "rules": [
            "official_dictionary 保留教育部修订本同形字的原始释义及读音，须按语境区分多音义项，不可混用；字典原文中的繁体不代表可以在候选中改用字库外字形",
            "字典上下文中的 related_characters 是明确标注的关联字资料，不得把所有义项移给锚字；supplemental_dictionary 为有来源的第三方字典补充，须结合语境核对。缺少顶层 definition 不等于完全没有字义资料，但所有来源均不足时不得自行补造",
            "quality_review_feedback 中是之前未通过或存疑的名称及具体问题，禁止重复名称或同类错误；反馈只用于改进，不得为了达标替旧名称强行辩解",
            "dictionary_definition_context 是 Unicode Unihan 的直接字义和读音记录，仅用于核查。不得把读音、部件或形近字的意义借给锚字；缺少 definition 时不可编造字义。文献意象也必须符合实际字义",
            "每个名称都必须恰好包含 anchor_character 一次或以上，且字符本身须参与自然语义；不能漏字或只在 meaning 中提到",
            "只用简体规范汉字；每个名称的字符数必须严格等于 allowed_lengths 中的一项，不得带标点、空格或数字",
            "每个长度分别尽量满足 requested_counts_per_length；数量不足时不要用重复、硬塞或低质量名称凑数",
            "名称应自然顺口、易读易记，适合长期公司品牌使用，并有具体、积极且不过度直白的意象",
            "避免生造词、句子感、产品说明感、空泛口号、机械堆字，以及只靠 meaning 才能解释通的组合",
            "同一批名称之间不得重复；也不得与 avoid_existing_names 中的名称重复",
            "候选之间应有实质差异；避免只换顺序、替换一个近义字、共享同一固定结构或仅作繁简/异体变换；锚字位置应有变化",
            "文献仅供灵感参考；确实使用原典意象时须准确理解其语义，不必强行为每个候选附会出处",
            "literature_context_examples 是本地文献检索到的字形上下文，可能包含异体字、旧义或无关语境；先判断与锚字是否相关，不相关时忽略，严禁把检索命中直接当作现代字义或名称",
            "避免常见成语、名人或现有知名品牌、锚字之外明显生僻的配字、谐音歧义、负面含义和行业通用套话",
            character_frequency_guidance(character),
            "meaning 用一句简洁、具体、可信的话说明字面组合如何形成品牌意象，不杜撰出处或夸大注册、商标可用性",
            "每个候选必须为名称中的每个不同汉字提供 character_roles 语义角色说明；锚字和其余汉字都要参与自然词义，机械拼接、仅作装饰或硬塞任一字的候选不得提交",
            "每个候选必须提供 1 到 5 的 naturalness 自评分；只有 4 或 5 分才可作为候选，数量不足时少交，不可降低标准",
            "输出前内部筛选并优先保留最自然、易读、语义清楚且彼此有区别的名称；不得输出筛选过程",
        ],
    }
    model = provider["model"] or os.environ.get("OPENAI_MODEL", PROVIDER_MODEL)
    if wire_api == "responses":
        request_payload = {
            "model": model,
            "stream": True,
            "input": [
                {"role": "system", "content": [{"type": "input_text", "text": system}]},
                {"role": "user", "content": [{"type": "input_text", "text": json.dumps(prompt, ensure_ascii=False)}]},
            ],
            "text": {"format": {"type": "json_object"}},
        }
    else:
        request_payload = {
            "model": model,
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
        with urllib.request.urlopen(request, timeout=300) as response:
            result = (_read_responses_stream(response) if wire_api == "responses"
                      else json.loads(response.read().decode("utf-8")))
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"AI 服务返回 HTTP {error.code}") from error
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
    total_count = payload.get("total_count")
    lengths = payload.get("lengths", [2, 3, 4])
    if type(count) is not int:
        raise ValueError("每种长度的数量必须是整数")
    if total_count is not None:
        if type(total_count) is not int or not 1 <= total_count <= 10_000:
            raise ValueError("单次生成总数必须是 1 到 10000 之间的整数")
    else:
        count = max(1, min(30, count))
    if not isinstance(lengths, list) or not lengths or any(
        type(length) is not int or length not in (2, 3, 4) for length in lengths
    ):
        raise ValueError("候选长度只能选择 2、3、4 字")
    lengths = sorted(set(lengths))
    characters = load_json(CHARACTERS_PATH)
    if not isinstance(character, str) or len(character) != 1 or character not in characters:
        raise ValueError("请选择字库中的一个汉字")

    provider_id = payload.get("providerId")
    if provider_id is not None and (not isinstance(provider_id, str) or not provider_id.strip()):
        raise ValueError("AI 服务商 ID 必须是非空字符串")
    provider = selected_ai_provider(provider_id)
    if provider is None:
        raise RuntimeError("尚未从 CC Switch 导入可用 AI 服务商，也未配置 OPENAI_API_KEY")
    characters_set = set(characters)
    tier_map = {char: tier_for(index) for index, char in enumerate(characters)}
    excluded = excluded_names()
    existing = load_existing_candidates(DB_PATH)
    existing_by_name = {item["name"]: item for item in existing}
    review_feedback = [
        {"name": item["name"], "reason": item.get("registryNote", "")}
        for item in existing if character in item["name"]
        and item.get("status") in {"质量复核未通过", "质量复核待定"}
    ][-20:]
    accepted: dict[str, str] = {}
    accepted_evidence: dict[str, dict] = {}
    attempted_names: dict[str, None] = {}
    requested_counts = None
    if total_count is not None:
        requested_counts = {length: total_count // len(lengths) for length in lengths}
        for length in lengths[:total_count % len(lengths)]:
            requested_counts[length] += 1
    raw_count = 0
    rounds_without_additions = 0
    warning = ""
    max_rounds = min(8, max(4, (total_count + 29) // 30 + 2)) if total_count is not None else 1
    for _ in range(max_rounds):
        if requested_counts is None:
            batch_counts = {length: count for length in lengths}
        else:
            batch_counts = {length: min(30, requested_counts[length] - sum(
                1 for name in accepted if len(name) == length
            )) for length in lengths}
            batch_counts = {length: amount for length, amount in batch_counts.items() if amount > 0}
            if not batch_counts:
                break
        try:
            raw_names = request_ai_names(
                character, max(batch_counts.values()), list(batch_counts), provider=provider,
                requested_counts=batch_counts,
                avoid_names=list(dict.fromkeys(
                    [name for name in existing_by_name if character in name] + list(attempted_names)
                )),
                review_feedback=review_feedback,
            )
        except (RuntimeError, OSError) as error:
            if not accepted:
                raise
            warning = str(error)
            break
        raw_count += len(raw_names)
        before = len(accepted)
        for item in raw_names:
            name = str(item.get("name", "")).strip()
            meaning = str(item.get("meaning", "")).strip()[:300]
            if character in name and len(name) in lengths and is_character_name(name, characters_set):
                attempted_names[name] = None
            if not has_quality_evidence(item, name):
                continue
            if len(name) not in batch_counts or character not in name:
                continue
            if not is_character_name(name, characters_set):
                continue
            if name in excluded or len(set(name)) < 2 or name in existing_by_name or name in accepted:
                continue
            if requested_counts is not None and sum(1 for accepted_name in accepted if len(accepted_name) == len(name)) >= requested_counts[len(name)]:
                continue
            accepted[name] = meaning or "由 AI 生成，待人工核对语义和品牌适配度。"
            accepted_evidence[name] = {
                "character_roles": item["character_roles"],
                "naturalness": item["naturalness"],
            }
        rounds_without_additions = rounds_without_additions + 1 if len(accepted) == before else 0
        if requested_counts is None or rounds_without_additions >= 4:
            break

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
    model = provider["model"] or os.environ.get("OPENAI_MODEL", PROVIDER_MODEL)
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
            "source": f"AI 生成 · {provider['name']} · {model}",
            "sourceType": "ai",
            "aiModel": model,
            "tags": ["AI 生成", f"{len(name)}字"],
            "scores": result.scores,
            "score": result.total,
            "reasons": result.reasons,
            "notes": [
                *result.notes,
                "质量初筛：各字语义角色=" + json.dumps(accepted_evidence[name]["character_roles"], ensure_ascii=False)
                + f"；自然度自评={accepted_evidence[name]['naturalness']}/5",
            ],
            "status": "待核验",
            "registryNote": "AI 生成候选；已通过各字语义角色及自然度初筛；仍需人工核对，并自行完成工商、商标和域名检索。",
        })

    if new_items:
        token_map = load_json(TOKENS_PATH)
        build_database(characters, token_map, new_items, db_path=DB_PATH)
    return {
        "items": new_items,
        "added": len(new_items),
        "model": model,
        "providerId": provider["id"],
        "providerName": provider["name"],
        "rejected": raw_count - len(new_items),
        "requested": total_count if total_count is not None else count * len(lengths),
        "warning": warning,
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
            elif parsed.path == "/api/ai/config":
                self.send_json(200, ai_config())
            else:
                super().do_GET()
        except (ValueError, sqlite3.Error) as error:
            self.send_json(400, {"error": str(error)})

    def do_POST(self) -> None:
        route = urlparse(self.path).path
        routes = {
            "/api/generate/by-character",
            "/api/ai/config/save",
            "/api/ai/config/select",
            "/api/ai/config/models",
            "/api/ai/config/test",
        }
        if route not in routes:
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
            if route == "/api/generate/by-character":
                result = generate_for_character(payload)
            elif route == "/api/ai/config/save":
                result = save_ai_provider(payload)
            elif route == "/api/ai/config/select":
                result = select_ai_provider(payload.get("providerId"))
            elif route == "/api/ai/config/models":
                result = refresh_ai_models(payload)
            else:
                result = test_ai_provider(payload)
            self.send_json(200, result)
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
