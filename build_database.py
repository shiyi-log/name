#!/usr/bin/env python3
"""Build the complete character database and the rule-generated name database."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from scoring import SCORE_RULES, CorpusStats, pinyin_profile, score_name


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
CHARACTERS_PATH = DATA_DIR / "characters-8105.json"
TOKENS_PATH = DATA_DIR / "tokens.json"
EXCLUDED_PATH = ROOT / "已排除名字.md"
DB_PATH = DATA_DIR / "names.db"
CANDIDATES_PATH = DATA_DIR / "candidates.json"

MAX_TIER_FOR_RULE_CANDIDATES = 2

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


def excluded_names() -> set[str]:
    if not EXCLUDED_PATH.exists():
        return set()
    text = EXCLUDED_PATH.read_text(encoding="utf-8")
    return set(re.findall(r"(?<![\u4e00-\u9fff])[\u4e00-\u9fff]{4}(?![\u4e00-\u9fff])", text))


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


def generate_candidates(characters: list[str], tokens: dict[str, list[str]], excluded: set[str]) -> tuple[list[dict], float]:
    character_set = set(characters)
    tier_map = {char: tier_for(index) for index, char in enumerate(characters)}
    common_chars = set(characters[:3500])
    clean_tokens = {
        category: valid_tokens(values, character_set, tier_map)
        for category, values in tokens.items()
    }

    combinations = list(iter_combinations(clean_tokens, excluded))
    stats = build_corpus_stats(combinations, characters)

    scored: list[tuple[float, dict]] = []
    for name, left, right, left_category, right_category, template in combinations:
        result = score_name(
            name, left, right, left_category, right_category,
            common_chars, stats, pinyin_profile(name),
        )
        scored.append((result.total, {
            "name": name,
            "leftToken": left,
            "rightToken": right,
            "template": template,
            "direction": "探索与科技" if "actions" in (left_category, right_category) else "东方与质感",
            "pitch": f"以“{left}”开启方向，以“{right}”留下空间。",
            "source": f"规则组合：{left} + {right}",
            "tags": [left_category.replace("s", ""), right_category.replace("s", "")],
            "scores": result.scores,
            "score": result.total,
            "reasons": result.reasons,
            "notes": result.notes,
            "status": "待核验",
            "registryNote": "规则内低冲突候选；未完成工商、商标、域名和社交账号的真实检索。",
        }))

    # 阈值取分位数，再用绝对下限兜底。分位数决定保留多少，
    # 下限只负责挡住明显不合格的名字——两者的方向是 max，不是 min。
    totals = sorted(item[0] for item in scored)
    index = min(len(totals) - 1, max(0, int(len(totals) * MINIMUM_SCORE_PERCENTILE)))
    threshold = max(MINIMUM_SCORE_FLOOR, totals[index]) if totals else MINIMUM_SCORE_FLOOR

    kept = [item for total, item in scored if total >= threshold]

    # 同一对词元的正反两种排列只保留分高的那个，去掉"云开入云 / 入云云开"这类镜像重复。
    deduped: dict[frozenset[str], dict] = {}
    for item in kept:
        key = frozenset((item["leftToken"], item["rightToken"]))
        current = deduped.get(key)
        if current is None or item["score"] > current["score"]:
            deduped[key] = item
    result = list(deduped.values())

    result.sort(
        key=lambda item: (
            -item["score"],
            -item["scores"]["distinctiveness"],
            -item["scores"]["readability"],
            item["name"],
        )
    )
    for index, item in enumerate(result, start=1):
        item["id"] = f"c{index:05d}"
    return result, round(threshold, 1)


def build_database(characters: list[str], tokens: dict[str, list[str]], candidates: list[dict]) -> None:
    tier_map = {char: tier_for(index) for index, char in enumerate(characters)}
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(
            """
            DROP TABLE IF EXISTS characters;
            DROP TABLE IF EXISTS tokens;
            DROP TABLE IF EXISTS name_candidates;
            DROP TABLE IF EXISTS combination_space;
            DROP TABLE IF EXISTS score_rules;
            CREATE TABLE characters (
                id INTEGER PRIMARY KEY,
                character TEXT NOT NULL UNIQUE,
                tier INTEGER NOT NULL,
                rank INTEGER NOT NULL
            );
            CREATE TABLE tokens (
                token TEXT NOT NULL,
                category TEXT NOT NULL,
                UNIQUE(token, category)
            );
            CREATE TABLE name_candidates (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
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
                registry_note TEXT NOT NULL
            );
            CREATE TABLE combination_space (
                label TEXT PRIMARY KEY,
                character_count INTEGER NOT NULL,
                combinations INTEGER NOT NULL,
                note TEXT NOT NULL
            );
            CREATE TABLE score_rules (
                dimension TEXT PRIMARY KEY,
                label TEXT NOT NULL,
                weight INTEGER NOT NULL,
                description TEXT NOT NULL
            );
            """
        )
        conn.executemany(
            "INSERT INTO characters (id, character, tier, rank) VALUES (?, ?, ?, ?)",
            [(index, char, tier_map[char], index) for index, char in enumerate(characters, start=1)],
        )
        conn.executemany(
            "INSERT INTO tokens (token, category) VALUES (?, ?)",
            list({
                (token, category)
                for category, values in tokens.items()
                for token in values
            }),
        )
        conn.executemany(
            """
            INSERT INTO name_candidates
                (id, name, left_token, right_token, template, direction, pitch, source,
                 tags_json, scores_json, total_score, reasons_json, notes_json, status, registry_note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    item["id"], item["name"], item["leftToken"], item["rightToken"], item["template"],
                    item["direction"], item["pitch"], item["source"], json.dumps(item["tags"], ensure_ascii=False),
                    json.dumps(item["scores"], ensure_ascii=False), item["score"], json.dumps(item["reasons"], ensure_ascii=False),
                    json.dumps(item["notes"], ensure_ascii=False),
                    item["status"], item["registryNote"],
                )
                for item in candidates
            ],
        )
        for label, count in [("一级常用字", 3500), ("一级+二级规范字", 6500), ("全部规范字", 8105)]:
            conn.execute(
                "INSERT INTO combination_space VALUES (?, ?, ?, ?)",
                (label, count, count**4, "四个位置均允许重复，顺序不同视为不同组合"),
            )
        conn.executemany("INSERT INTO score_rules VALUES (?, ?, ?, ?)", SCORE_RULES)
        conn.commit()


def main() -> None:
    characters = load_json(CHARACTERS_PATH)
    tokens = load_json(TOKENS_PATH)
    candidates, threshold = generate_candidates(characters, tokens, excluded_names())
    CANDIDATES_PATH.write_text(json.dumps(candidates, ensure_ascii=False, indent=2), encoding="utf-8")
    build_database(characters, tokens, candidates)
    print(f"Characters: {len(characters)}")
    print(f"Rule candidates: {len(candidates)}")
    print(f"Dynamic score threshold: {threshold}")
    print(f"Database: {DB_PATH}")


if __name__ == "__main__":
    main()
