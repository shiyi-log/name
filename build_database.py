#!/usr/bin/env python3
"""从整理后的来源增量扩充带评分的公司名称数据库。"""

from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import itertools
import json
import re
import sqlite3
import time
import urllib.request
from urllib.parse import quote
from pathlib import Path

from scoring import SCORE_RULES, CorpusStats, pinyin_profile, score_name


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
CHARACTERS_PATH = DATA_DIR / "characters-8105.json"
TOKENS_PATH = DATA_DIR / "tokens.json"
EXCLUDED_PATH = ROOT / "已排除名字.md"
DB_PATH = DATA_DIR / "names.db"
HISTORICAL_CANDIDATES_PATH = ROOT / "restored_candidates_all_history.txt"
LEGACY_GENERATION_STATE_PATH = DATA_DIR / "generation_state.json"

MAX_TIER_FOR_RULE_CANDIDATES = 2
POETRY_REPOSITORY = "https://github.com/chinese-poetry/chinese-poetry"
POETRY_API_ROOT = "https://api.github.com/repos/chinese-poetry/chinese-poetry"
POETRY_RAW_ROOT = "https://raw.githubusercontent.com/chinese-poetry/chinese-poetry/master"
POETRY_FOLDER = "全唐诗"

# 分数下限按分位数动态确定，避免像旧版那样写死绝对值导致候选数量随评分标准漂移。
# 下限只做兜底，防止整体评分口径下调时把明显不合格的名字也放进来；
# 它不能高于分位数算出来的阈值，否则分位数就失效了。
MINIMUM_SCORE_PERCENTILE = 0.60
MINIMUM_SCORE_FLOOR = 60.0

TEMPLATES = {
    "action-image": ("actions", "images"),
    "image-action": ("images", "actions"),
    "action-abstract": ("actions", "abstracts"),
    "abstract-action": ("abstracts", "actions"),
    "image-abstract": ("images", "abstracts"),
    "abstract-image": ("abstracts", "images"),
}


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_existing_candidates(db_path: Path = DB_PATH) -> list[dict]:
    if not db_path.exists():
        return []
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            columns = {row[1] for row in conn.execute("PRAGMA table_info(name_candidates)")}
            source_type = "source_type" if "source_type" in columns else "'' AS source_type"
            ai_model = "ai_model" if "ai_model" in columns else "'' AS ai_model"
            rows = conn.execute(
                f"""SELECT id, name, left_token, right_token, template, direction, pitch, source,
                           tags_json, scores_json, total_score, reasons_json, notes_json, status, registry_note,
                           {source_type}, {ai_model}
                    FROM name_candidates"""
            ).fetchall()
    except sqlite3.Error:
        return []
    return [
        {
            "id": row["id"],
            "name": row["name"],
            "leftToken": row["left_token"],
            "rightToken": row["right_token"],
            "template": row["template"],
            "direction": row["direction"],
            "pitch": row["pitch"],
            "source": row["source"],
            "sourceType": row["source_type"] or source_type_for({
                "template": row["template"], "aiModel": row["ai_model"],
            }),
            "aiModel": row["ai_model"],
            "tags": json.loads(row["tags_json"]),
            "scores": json.loads(row["scores_json"]),
            "score": row["total_score"],
            "reasons": json.loads(row["reasons_json"]),
            "notes": json.loads(row["notes_json"]),
            "status": row["status"],
            "registryNote": row["registry_note"],
        }
        for row in rows
    ]


def stable_candidate_id(name: str) -> str:
    return "c" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]


def source_type_for(item: dict) -> str:
    template = item.get("template", "")
    if template == "literature-curated":
        return "literature"
    if item.get("aiModel") or template.startswith("ai-"):
        return "ai"
    if template == "poetry-seed":
        return "poetry"
    if template == "historical-seed":
        return "historical"
    return "rule"


def excluded_names() -> set[str]:
    if not EXCLUDED_PATH.exists():
        return set()
    text = EXCLUDED_PATH.read_text(encoding="utf-8")
    return set(re.findall(r"(?<![\u4e00-\u9fff])[\u4e00-\u9fff]{2,4}(?![\u4e00-\u9fff])", text))


def tier_for(index: int) -> int:
    if index < 3500:
        return 1
    if index < 6500:
        return 2
    return 3


def valid_tokens(tokens: list[str], character_set: set[str], tier_map: dict[str, int]) -> list[str]:
    result = []
    for token in dict.fromkeys(tokens):
        if len(token) != 2 or not set(token).issubset(character_set):
            continue
        if max(tier_map[char] for char in token) > MAX_TIER_FOR_RULE_CANDIDATES:
            continue
        result.append(token)
    return result


def iter_combinations(clean_tokens: dict[str, list[str]], excluded: set[str]):
    """生成全部通过结构规则的组合，返回 (name, left, right, left_cat, right_cat)。"""
    seen: set[str] = set()
    for template, (left_category, right_category) in TEMPLATES.items():
        for left in clean_tokens[left_category]:
            for right in clean_tokens[right_category]:
                name = left + right
                if left == right or name in excluded or len(set(name)) < 3:
                    continue
                if name in seen:
                    continue
                seen.add(name)
                yield name, left, right, left_category, right_category, template


def iter_short_combinations(clean_tokens: dict[str, list[str]], excluded: set[str]):
    """生成原始词元，以及通过共享一字连接而成的三字组合。"""
    seen: set[str] = set()
    categorized_tokens = [
        (token, category)
        for category in ("actions", "images", "abstracts")
        for token in clean_tokens[category]
    ]
    for token, category in categorized_tokens:
        if token in excluded or token in seen:
            continue
        seen.add(token)
        yield token, token, "", category, "abstracts", "lexicon-2"

    tokens_by_initial: dict[str, list[tuple[str, str]]] = {}
    for token, category in categorized_tokens:
        tokens_by_initial.setdefault(token[0], []).append((token, category))
    for left, left_category in categorized_tokens:
        for right, right_category in tokens_by_initial.get(left[-1], []):
            name = left + right[1:]
            if name in excluded or name in seen or len(set(name)) < 2:
                continue
            seen.add(name)
            yield name, left, right, left_category, right_category, "overlap-3"


def load_historical_candidates(
    characters: list[str], excluded: set[str], existing_names: set[str]
) -> dict[str, str]:
    """加载恢复的历史名称，并作为单独标记的种子来源。"""
    if not HISTORICAL_CANDIDATES_PATH.exists():
        return {}

    character_set = set(characters)
    tier_map = {char: tier_for(index) for index, char in enumerate(characters)}
    names: dict[str, str] = {}
    for line in HISTORICAL_CANDIDATES_PATH.read_text(encoding="utf-8").splitlines():
        parts = line.split(" - ", 2)
        if len(parts) < 2:
            continue
        name = parts[0].strip()
        if len(name) != 4 or not set(name).issubset(character_set):
            continue
        if max(tier_map[char] for char in name) > MAX_TIER_FOR_RULE_CANDIDATES:
            continue
        if name in excluded or name in existing_names or len(set(name)) < 3:
            continue
        explanation = parts[2].strip() if len(parts) > 2 else ""
        names.setdefault(name, explanation)
    return names


def discover_poetry_files() -> list[dict[str, str]]:
    """发现上游现有档案，使后续新增文件无需修改代码即可处理。"""
    found: list[dict[str, str]] = []
    page = 1
    while True:
        url = (
            f"{POETRY_API_ROOT}/contents/{quote(POETRY_FOLDER)}"
            f"?ref=master&per_page=1000&page={page}"
        )
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "name-research-local-generator"},
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            entries = json.loads(response.read().decode("utf-8"))
        if not isinstance(entries, list):
            raise RuntimeError(f"Unexpected poetry source listing: {url}")
        found.extend(
            {"path": entry["path"], "sha": entry["sha"]}
            for entry in entries
            if entry.get("type") == "file"
            and entry.get("name", "").endswith(".json")
            and entry.get("name", "").startswith(("poet.tang.", "poet.song."))
            and entry.get("sha")
        )
        if len(entries) < 1000:
            break
        page += 1
    return sorted(found, key=lambda item: item["path"])


def select_unprocessed_poetry_files(
    state: dict, manifest: list[dict[str, str]], count: int
) -> list[dict[str, str]]:
    processed = state.get("processed_poetry_files", {})
    return [
        item for item in manifest
        if processed.get(item["path"]) != item["sha"]
    ][:count]


def fetch_poetry_batch(
    state: dict,
    shard_count: int,
    characters: list[str],
    excluded: set[str],
    existing_names: set[str],
    manifest: list[dict[str, str]] | None = None,
) -> tuple[dict[str, dict[str, str]], dict]:
    """获取下一批未处理的上游文件，并提取二至四字连续片段。"""
    seeds: dict[str, dict[str, str]] = {}
    next_state = dict(state)
    next_state["processed_poetry_files"] = dict(state.get("processed_poetry_files", {}))
    character_set = set(characters)
    tier_map = {char: tier_for(index) for index, char in enumerate(characters)}
    manifest = discover_poetry_files() if manifest is None else manifest
    batch = select_unprocessed_poetry_files(next_state, manifest, shard_count)
    processed = 0

    for source_file in batch:
        source_path = source_file["path"]
        source_url = f"{POETRY_RAW_ROOT}/{quote(source_path, safe='/')}"
        with urllib.request.urlopen(source_url, timeout=60) as response:
            poems = json.loads(response.read().decode("utf-8"))

        for poem in poems:
            title = poem.get("title", "未题")
            author = poem.get("author", "佚名")
            for paragraph in poem.get("paragraphs", []):
                for phrase_run in re.findall(r"[\u4e00-\u9fff]+", paragraph):
                    for length in (2, 3, 4):
                        for index in range(max(0, len(phrase_run) - length + 1)):
                            name = phrase_run[index : index + length]
                            minimum_unique = 3 if length == 4 else 2
                            if len(set(name)) < minimum_unique or name in excluded or name in existing_names:
                                continue
                            if not set(name).issubset(character_set):
                                continue
                            if max(tier_map[char] for char in name) > MAX_TIER_FOR_RULE_CANDIDATES:
                                continue
                            seeds.setdefault(name, {
                                "pitch": f"取自《{title}》原句“{name}”；需结合原诗语境判断是否适合字号。",
                                "source": f"古诗原句片段：{title}（{author}），{source_path}，{POETRY_REPOSITORY}",
                            })
        next_state["processed_poetry_files"][source_path] = source_file["sha"]
        next_state["last_source_file"] = source_path
        processed += 1

    next_state["processed_poetry_shards"] = next_state.get("processed_poetry_shards", 0) + processed
    next_state["last_batch_files"] = [item["path"] for item in batch[:processed]]
    return seeds, next_state


def build_corpus_stats(combinations: list[tuple], characters: list[str]) -> CorpusStats:
    """先扫一遍全部组合，用真实分布计算稀有度。"""
    stats = CorpusStats()
    stats.char_rank = {char: index for index, char in enumerate(characters)}
    names = [item[0] for item in combinations]
    stats.build(
        names,
        [item[1] for item in combinations],
        [item[2] for item in combinations],
    )
    return stats


def generate_candidates(
    characters: list[str],
    tokens: dict[str, list[str]],
    excluded: set[str],
    existing_candidates: list[dict],
    poetry_seeds: dict[str, dict[str, str]],
) -> tuple[list[dict], dict[str, float], dict[str, int], int]:
    character_set = set(characters)
    tier_map = {char: tier_for(index) for index, char in enumerate(characters)}
    common_chars = set(characters[:3500])
    existing_by_name = {item["name"]: item for item in existing_candidates}
    clean_tokens = {
        category: valid_tokens(values, character_set, tier_map)
        for category, values in tokens.items()
    }

    combinations = list(iter_combinations(clean_tokens, excluded))
    combinations.extend(iter_short_combinations(clean_tokens, excluded))
    rule_names = {item[0] for item in combinations}
    historical = load_historical_candidates(
        characters, excluded, rule_names | set(existing_by_name)
    )
    token_categories: dict[str, set[str]] = {}
    for category, values in clean_tokens.items():
        for token in values:
            token_categories.setdefault(token, set()).add(category)

    def category_for(token: str) -> str:
        for category in ("actions", "images", "abstracts"):
            if category in token_categories.get(token, set()):
                return category
        return "historical"

    combinations_by_name = {item[0]: item for item in combinations}
    for name in historical:
        combinations_by_name.setdefault(
            name,
            (name, name[:2], name[2:], category_for(name[:2]), category_for(name[2:]), "historical-seed"),
        )
    for name in poetry_seeds:
        if name not in excluded and name not in combinations_by_name and name not in existing_by_name:
            combinations_by_name[name] = (
                name, name[:2], name[2:], category_for(name[:2]), category_for(name[2:]), "poetry-seed"
            )

    for name, item in existing_by_name.items():
        categories = TEMPLATES.get(item["template"], (category_for(item["leftToken"]), category_for(item["rightToken"])))
        combinations_by_name[name] = (
            name, item["leftToken"], item["rightToken"], categories[0], categories[1], item["template"]
        )

    all_combinations = list(combinations_by_name.values())
    stats = build_corpus_stats(all_combinations, characters)

    scored: list[tuple[float, dict]] = []
    for name, left, right, left_category, right_category, template in all_combinations:
        previous = existing_by_name.get(name)
        is_historical = template == "historical-seed"
        is_poetry = template == "poetry-seed"
        result = score_name(
            name, left, right, left_category, right_category,
            common_chars, stats, pinyin_profile(name),
        )
        tags = list(dict.fromkeys(
            category.replace("s", "")
            for category in (left_category, right_category)
            if category != "historical"
        ))
        if is_historical and previous is None:
            tags.append("历史候选")
        if is_poetry and previous is None:
            tags.append("古诗候选")
        if previous is not None:
            tags = previous.get("tags", tags)

        if previous is not None:
            pitch = previous.get("pitch", "")
            source = previous.get("source", "")
            registry_note = previous.get("registryNote", "")
            direction = previous.get("direction", "东方与质感")
        elif is_historical:
            pitch = historical[name] or f"以“{left}”开启方向，以“{right}”留下空间。"
            source = "历史恢复候选；原始筛选方式未记录，已按当前规则重评分"
            registry_note = "历史候选仅按当前文本规则筛选；未完成工商、商标、域名和社交账号的真实检索。"
            direction = "探索与科技" if "actions" in (left_category, right_category) else "东方与质感"
        elif is_poetry:
            pitch = poetry_seeds[name]["pitch"]
            source = poetry_seeds[name]["source"]
            registry_note = "源自诗词原句片段；评分仅作启发，未完成工商、商标、域名和社交账号检索。"
            direction = "探索与科技" if "actions" in (left_category, right_category) else "东方与质感"
        elif template == "lexicon-2":
            pitch = f"词库原词“{name}”，含义清晰，适合作为简短名称方向。"
            source = f"基础词元：{name}（{left_category}）"
            registry_note = "词元候选仅按当前文本规则筛选；未完成工商、商标、域名和社交账号的真实检索。"
            direction = "探索与科技" if left_category == "actions" else "东方与质感"
        elif template == "overlap-3":
            pitch = f"由“{left}”与“{right}”共享一字连接，形成三字词。"
            source = f"一字重叠组合：{left} + {right}"
            registry_note = "重叠词元候选仅作启发；未完成工商、商标、域名和社交账号的真实检索。"
            direction = "探索与科技" if "actions" in (left_category, right_category) else "东方与质感"
        else:
            pitch = f"以“{left}”开启方向，以“{right}”留下空间。"
            source = f"规则组合：{left} + {right}"
            registry_note = "规则内低冲突候选；未完成工商、商标、域名和社交账号的真实检索。"
            direction = "探索与科技" if "actions" in (left_category, right_category) else "东方与质感"
        scored.append((result.total, {
            "name": name,
            "leftToken": left,
            "rightToken": right,
            "template": template,
            "direction": direction,
            "pitch": pitch,
            "source": source,
            "sourceType": previous.get("sourceType", source_type_for(previous)) if previous else source_type_for({"template": template}),
            "aiModel": previous.get("aiModel", "") if previous else "",
            "tags": tags,
            "scores": result.scores,
            "score": result.total,
            "reasons": result.reasons,
            "notes": (
                previous.get("notes", result.notes)
                if previous and template == "literature-curated" else result.notes
            ),
            "status": previous.get("status", "待核验") if previous else "待核验",
            "registryNote": registry_note,
        }))

    # 按首字分组排名，避免候选多的组把候选少的组挤到全库阈值以下。
    # 分数下限仍会排除明显较弱的候选。
    totals_by_initial: dict[str, list[float]] = {}
    for total, item in scored:
        totals_by_initial.setdefault(item["name"][0], []).append(total)
    thresholds: dict[str, float] = {}
    for initial, values in totals_by_initial.items():
        values.sort()
        index = min(len(values) - 1, max(0, int(len(values) * MINIMUM_SCORE_PERCENTILE)))
        thresholds[initial] = max(MINIMUM_SCORE_FLOOR, values[index])

    existing_names = set(existing_by_name)
    kept = [
        item for total, item in scored
        if item["name"] in existing_names or total >= thresholds[item["name"][0]]
    ]

    # 保留已有记录和独立来源片段；镜像去重只应用于规则词元对。
    by_name: dict[str, dict] = {}
    for item in kept:
        current = by_name.get(item["name"])
        if current is None or item["score"] > current["score"]:
            by_name[item["name"]] = item

    existing_rule_items = []
    existing_rule_pairs: set[frozenset[str]] = set()
    for item in by_name.values():
        if item["template"] not in {"historical-seed", "poetry-seed"} and item["name"] in existing_names:
            existing_rule_items.append(item)
            existing_rule_pairs.add(frozenset((item["leftToken"], item["rightToken"])))

    deduped: dict[frozenset[str], dict] = {}
    independent_items = []
    for item in by_name.values():
        if item["template"] in {"historical-seed", "poetry-seed"}:
            independent_items.append(item)
            continue
        if item["name"] in existing_names:
            continue
        key = frozenset((item["leftToken"], item["rightToken"]))
        if key in existing_rule_pairs:
            continue
        current = deduped.get(key)
        if current is None or item["score"] > current["score"]:
            deduped[key] = item
    result = existing_rule_items + list(deduped.values()) + independent_items

    result.sort(
        key=lambda item: (
            -item["score"],
            -item["scores"]["distinctiveness"],
            -item["scores"]["readability"],
            item["name"],
        )
    )
    for item in result:
        item["id"] = existing_by_name.get(item["name"], {}).get("id") or stable_candidate_id(item["name"])
    source_counts = {
        "rule": sum(item["template"] in TEMPLATES or item["template"] in {"lexicon-2", "overlap-3"} for item in result),
        "historical": sum(item["template"] == "historical-seed" for item in result),
        "poetry": sum(item["template"] == "poetry-seed" for item in result),
        "literature": sum(item["template"] == "literature-curated" for item in result),
    }
    added_count = sum(item["name"] not in existing_names for item in result)
    return result, {char: round(value, 1) for char, value in thresholds.items()}, source_counts, added_count


def legacy_generation_state() -> dict:
    if not LEGACY_GENERATION_STATE_PATH.exists():
        return {}
    try:
        state = json.loads(LEGACY_GENERATION_STATE_PATH.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def load_generation_state(db_path: Path = DB_PATH) -> dict:
    state = {
        "source_repository": POETRY_REPOSITORY,
        "source_branch": "master",
        "processed_poetry_files": {},
        "processed_poetry_shards": 0,
    }
    legacy = legacy_generation_state()
    state.update({key: value for key, value in legacy.items() if key != "next_poetry_shard"})
    processed = dict(state.get("processed_poetry_files", {}))
    if db_path.exists():
        try:
            with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "poetry_sources" in tables:
                    processed.update(conn.execute("SELECT path, sha FROM poetry_sources"))
                if "generation_metadata" in tables:
                    last_source = conn.execute(
                        "SELECT value FROM generation_metadata WHERE key='last_source_file'"
                    ).fetchone()
                    if last_source:
                        state["last_source_file"] = last_source[0]
        except sqlite3.Error:
            pass
    state["processed_poetry_files"] = processed
    state["processed_poetry_shards"] = len(processed)
    return state


def build_database(
    characters: list[str],
    tokens: dict[str, list[str]],
    candidates: list[dict],
    db_path: Path = DB_PATH,
    generation_state: dict | None = None,
) -> None:
    tier_map = {char: tier_for(index) for index, char in enumerate(characters)}
    previous_state = load_generation_state(db_path)
    processed_files = dict(previous_state["processed_poetry_files"])
    if generation_state:
        processed_files.update(generation_state.get("processed_poetry_files", {}))
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS characters (
                id INTEGER PRIMARY KEY,
                character TEXT NOT NULL UNIQUE,
                tier INTEGER NOT NULL,
                rank INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tokens (
                token TEXT NOT NULL,
                category TEXT NOT NULL,
                UNIQUE(token, category)
            );
            CREATE TABLE IF NOT EXISTS name_candidates (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                name_length INTEGER NOT NULL DEFAULT 4,
                left_token TEXT NOT NULL,
                right_token TEXT NOT NULL,
                template TEXT NOT NULL,
                direction TEXT NOT NULL,
                pitch TEXT NOT NULL,
                source TEXT NOT NULL,
                tags_json TEXT NOT NULL,
                scores_json TEXT NOT NULL,
                total_score REAL NOT NULL,
                reasons_json TEXT NOT NULL,
                notes_json TEXT NOT NULL,
                status TEXT NOT NULL,
                registry_note TEXT NOT NULL,
                source_type TEXT NOT NULL DEFAULT 'rule',
                ai_model TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS combination_space (
                label TEXT PRIMARY KEY,
                character_count INTEGER NOT NULL,
                combinations INTEGER NOT NULL,
                note TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS score_rules (
                dimension TEXT PRIMARY KEY,
                label TEXT NOT NULL,
                weight INTEGER NOT NULL,
                description TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS poetry_sources (
                path TEXT PRIMARY KEY,
                sha TEXT NOT NULL,
                processed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS generation_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        candidate_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(name_candidates)")
        }
        for column, declaration in (
            ("name_length", "INTEGER NOT NULL DEFAULT 4"),
            ("source_type", "TEXT NOT NULL DEFAULT 'rule'"),
            ("ai_model", "TEXT NOT NULL DEFAULT ''"),
            ("created_at", "TEXT NOT NULL DEFAULT ''"),
            ("updated_at", "TEXT NOT NULL DEFAULT ''"),
        ):
            if column not in candidate_columns:
                conn.execute(f"ALTER TABLE name_candidates ADD COLUMN {column} {declaration}")
        poetry_columns = {row[1] for row in conn.execute("PRAGMA table_info(poetry_sources)")}
        if "processed_at" not in poetry_columns:
            conn.execute("ALTER TABLE poetry_sources ADD COLUMN processed_at TEXT NOT NULL DEFAULT ''")
        conn.execute("UPDATE name_candidates SET name_length=length(name) WHERE name_length != length(name)")
        conn.execute(
            """UPDATE name_candidates SET source_type=CASE
               WHEN template='literature-curated' THEN 'literature'
               WHEN template='poetry-seed' THEN 'poetry'
               WHEN template='historical-seed' THEN 'historical'
               WHEN template LIKE 'ai-%' THEN 'ai'
               ELSE 'rule' END
               WHERE source_type != CASE
                 WHEN template='literature-curated' THEN 'literature'
                 WHEN template='poetry-seed' THEN 'poetry'
                 WHEN template='historical-seed' THEN 'historical'
                 WHEN template LIKE 'ai-%' THEN 'ai'
                 ELSE 'rule' END"""
        )

        conn.executemany(
            """INSERT INTO poetry_sources (path, sha, processed_at) VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(path) DO UPDATE SET sha=excluded.sha, processed_at=CURRENT_TIMESTAMP
            WHERE poetry_sources.sha != excluded.sha""",
            list(processed_files.items()),
        )
        last_source = (generation_state or {}).get("last_source_file") or previous_state.get("last_source_file")
        if last_source:
            conn.execute(
                "INSERT OR REPLACE INTO generation_metadata (key, value) VALUES ('last_source_file', ?)",
                (last_source,),
            )
        conn.executemany(
            """INSERT INTO characters (id, character, tier, rank) VALUES (?, ?, ?, ?)
            ON CONFLICT(character) DO UPDATE SET tier=excluded.tier, rank=excluded.rank""",
            [(index, char, tier_map[char], index) for index, char in enumerate(characters, start=1)],
        )
        token_rows = {
            (token, category)
            for category, values in tokens.items()
            for token in values
        }
        token_rows.update(
            (token, item["template"])
            for item in candidates
            if item["template"] in {"historical-seed", "poetry-seed"}
            for token in (item["leftToken"], item["rightToken"])
        )
        conn.executemany(
            "INSERT OR IGNORE INTO tokens (token, category) VALUES (?, ?)",
            list(token_rows),
        )
        conn.executemany(
            """
            INSERT INTO name_candidates
                (id, name, name_length, left_token, right_token, template, direction, pitch, source,
                 tags_json, scores_json, total_score, reasons_json, notes_json, status, registry_note,
                 source_type, ai_model)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                name_length=excluded.name_length,
                left_token=excluded.left_token,
                right_token=excluded.right_token,
                template=excluded.template,
                direction=excluded.direction,
                pitch=excluded.pitch,
                source=excluded.source,
                tags_json=excluded.tags_json,
                scores_json=excluded.scores_json,
                total_score=excluded.total_score,
                reasons_json=excluded.reasons_json,
                notes_json=excluded.notes_json,
                source_type=excluded.source_type,
                ai_model=excluded.ai_model,
                updated_at=CURRENT_TIMESTAMP
            WHERE name_candidates.status = excluded.status
              AND name_candidates.registry_note = excluded.registry_note
            """,
            [
                (
                    item["id"], item["name"], len(item["name"]), item["leftToken"], item["rightToken"], item["template"],
                    item["direction"], item["pitch"], item["source"], json.dumps(item["tags"], ensure_ascii=False),
                    json.dumps(item["scores"], ensure_ascii=False), item["score"], json.dumps(item["reasons"], ensure_ascii=False),
                    json.dumps(item["notes"], ensure_ascii=False),
                    item["status"], item["registryNote"], item.get("sourceType", source_type_for(item)), item.get("aiModel", ""),
                )
                for item in candidates
            ],
        )
        for label, count in [("一级常用字", 3500), ("一级+二级规范字", 6500), ("全部规范字", 8105)]:
            conn.execute(
                """INSERT INTO combination_space VALUES (?, ?, ?, ?)
                ON CONFLICT(label) DO UPDATE SET
                    character_count=excluded.character_count,
                    combinations=excluded.combinations,
                    note=excluded.note""",
                (label, count, count**4, "四个位置均允许重复，顺序不同视为不同组合"),
            )
        conn.executemany(
            """INSERT INTO score_rules VALUES (?, ?, ?, ?)
            ON CONFLICT(dimension) DO UPDATE SET
                label=excluded.label,
                weight=excluded.weight,
                description=excluded.description""",
            SCORE_RULES,
        )
        conn.commit()


def run_once(poetry_shards: int, no_grow: bool) -> None:
    characters = load_json(CHARACTERS_PATH)
    tokens = load_json(TOKENS_PATH)
    existing_candidates = load_existing_candidates()
    state = load_generation_state()
    if no_grow:
        poetry_seeds, next_state = {}, state
        processed_files: list[str] = []
    else:
        poetry_seeds, next_state = fetch_poetry_batch(
            state,
            poetry_shards,
            characters,
            excluded_names(),
            {item["name"] for item in existing_candidates},
        )
        processed_files = next_state.get("last_batch_files", [])

    if not processed_files and not no_grow:
        print(f"New poetry files: 0; {len(existing_candidates)} candidates retained")
        print("No unseen source files. Use --watch to monitor upstream additions or --no-grow to rescore.")
        return

    candidates, thresholds, source_counts, added_count = generate_candidates(
        characters, tokens, excluded_names(), existing_candidates, poetry_seeds
    )
    build_database(
        characters,
        tokens,
        candidates,
        generation_state=next_state if processed_files else None,
    )
    print(f"Characters: {len(characters)}")
    print(f"Rule candidates: {source_counts['rule']}")
    print(f"Historical candidates: {source_counts['historical']}")
    print(f"Poetry candidates: {source_counts['poetry']}")
    print(f"Literature candidates: {source_counts['literature']}")
    print(f"New candidates this run: {added_count}")
    print(f"Poetry files processed: {len(processed_files)}")
    if processed_files:
        print(f"Last source file: {processed_files[-1]}")
    print(f"Total candidates: {len(candidates)}")
    print(f"Distinct initial characters: {len({item['name'][0] for item in candidates})}")
    print(
        "Per-initial score thresholds: "
        f"{min(thresholds.values()):.1f}..{max(thresholds.values()):.1f} "
        f"across {len(thresholds)} initials"
    )
    print(f"Database: {DB_PATH}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Grow the scored name database from newly discovered poetry files.")
    parser.add_argument(
        "--poetry-shards",
        type=int,
        default=1,
        help="maximum number of newly discovered poetry files to process per batch",
    )
    parser.add_argument("--no-grow", action="store_true", help="rescore and update the database without fetching poetry")
    parser.add_argument("--watch", action="store_true", help="keep checking for upstream additions and process each batch")
    parser.add_argument("--interval-seconds", type=float, default=300, help="delay between --watch checks")
    args = parser.parse_args()
    if args.poetry_shards < 1:
        parser.error("--poetry-shards must be at least 1")
    if args.interval_seconds <= 0:
        parser.error("--interval-seconds must be greater than 0")
    if args.watch and args.no_grow:
        parser.error("--watch cannot be combined with --no-grow")

    while True:
        run_once(args.poetry_shards, args.no_grow)
        if not args.watch:
            break
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    main()
