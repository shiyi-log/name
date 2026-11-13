#!/usr/bin/env python3
"""Generate unique AI naming candidates for every character, with resumable checkpoints."""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import api_server  # noqa: E402

STATE_PATH = ROOT / "data" / "character_generation_state.json"
TARGET_PER_CHARACTER = 500
MAX_GENERATION_BATCH = 90
LENGTHS = [2, 3, 4]
TRANSIENT_ERRORS = (TimeoutError, ConnectionError, urllib.error.URLError, OSError)
TRANSIENT_MESSAGES = (
    "timed out", "temporary failure", "connection reset", "connection refused",
    "network is unreachable", "http 429", "http 502", "http 503", "http 504",
)


def count_names_for_characters(names: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for name in set(names):
        for character in set(name):
            counts[character] = counts.get(character, 0) + 1
    return counts


def parse_character_roles(notes_json: str) -> dict[str, str]:
    try:
        notes = json.loads(notes_json)
    except json.JSONDecodeError:
        return {}
    for note in notes:
        marker = "质量初筛：各字语义角色="
        if note.startswith(marker):
            try:
                roles, _ = json.JSONDecoder().raw_decode(note[len(marker):])
                return roles if isinstance(roles, dict) else {}
            except json.JSONDecodeError:
                return {}
    return {}


def count_names_for_character_set(records: list, characters: list[str]) -> dict[str, int]:
    allowed = set(characters)
    unique = {}
    for record in records:
        if isinstance(record, str):
            unique[record] = ("rule", {})
            continue
        name, source_type, roles = record
        unique.setdefault(name, (source_type, roles))
    counts = {character: 0 for character in characters}
    for name, (source_type, roles) in unique.items():
        if source_type != "ai":
            covered = set(name) & allowed
        else:
            covered = {
                character for character, role in roles.items()
                if character in name and character in allowed and isinstance(role, str) and len(role.strip()) >= 6
            }
        for character in covered:
            counts[character] += 1
    return counts


def pending_targets(characters: list[str], counts: dict[str, int], target: int) -> list[tuple[str, int]]:
    targets = [
        (character, counts.get(character, 0))
        for character in characters
        if counts.get(character, 0) < target
    ]
    targets.sort(key=lambda item: (item[1] != 0, item[1], characters.index(item[0])))
    return [(character, target - current) for character, current in targets]


def targets_for_cycle(
    targets: list[tuple[str, int]], retry_after: dict[str, float], *, now: float,
) -> list[tuple[str, int]]:
    return [(character, deficit) for character, deficit in targets if retry_after.get(character, 0) <= now]


def phase_targets(
    characters: list[str], counts: dict[str, int], target: int,
    all_characters: list[str] | None = None,
) -> tuple[str, list[tuple[str, int]]]:
    pending = pending_targets(characters, counts, target)
    uncovered = [item for item in pending if counts.get(item[0], 0) == 0]
    underfilled = [item for item in pending if counts.get(item[0], 0) > 0]
    if uncovered:
        interleaved = []
        for index in range(max(len(uncovered), len(underfilled))):
            if index < len(uncovered):
                interleaved.append(uncovered[index])
            if index < len(underfilled):
                interleaved.append(underfilled[index])
        return "coverage_and_fill", interleaved
    return "fill_to_500", underfilled


def request_batch_size(phase: str, deficit: int) -> int:
    if phase == "coverage":
        return MAX_GENERATION_BATCH
    return min(MAX_GENERATION_BATCH, max(0, deficit))


def generation_complete(counts: dict[str, int], characters: list[str], target: int) -> bool:
    return all(counts.get(character, 0) >= target for character in characters)


def should_retry_idle_cycle(counts: dict[str, int], characters: list[str], target: int) -> bool:
    return not generation_complete(counts, characters, target)


def should_wait_for_other_ranges(
    counts: dict[str, int], characters: list[str], all_characters: list[str], target: int,
) -> bool:
    return generation_complete(counts, characters, target) and not generation_complete(counts, all_characters, target)


def select_character_range(characters: list[str], start: int, end: int | None) -> list[str]:
    if start < 0 or (end is not None and end < start):
        raise ValueError("字库序号范围无效")
    return characters[start:end]


def load_character_names() -> list[str]:
    with sqlite3.connect(api_server.DB_PATH) as connection:
        rows = connection.execute(
            "SELECT name, source_type, registry_note, notes_json FROM name_candidates "
            "WHERE name_length BETWEEN 2 AND 4 AND status NOT IN ('质量复核未通过', '质量复核待定')"
        )
        names = []
        seen_names = set()
        for name, source_type, registry_note, notes_json in rows:
            if source_type != "ai":
                continue
            roles = parse_character_roles(notes_json)
            rating_match = re.search(r"；自然度自评=([0-9]+(?:[.][0-9]+)?)/5", notes_json)
            rating = float(rating_match.group(1)) if rating_match else None
            if api_server.has_quality_evidence({'character_roles': roles, 'naturalness': rating}, name):
                if name in seen_names:
                    continue
                seen_names.add(name)
                names.append((name, source_type, roles))
        return names


def load_state(path: Path) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def is_transient_error(error: Exception) -> bool:
    return isinstance(error, TRANSIENT_ERRORS) or (
        isinstance(error, RuntimeError)
        and any(message in str(error).lower() for message in TRANSIENT_MESSAGES)
    )


def request_with_retries(payload: dict, attempts: int = 3) -> dict:
    last_error = None
    for attempt in range(attempts):
        try:
            return api_server.generate_for_character(payload)
        except Exception as error:
            if not is_transient_error(error):
                raise
            last_error = error
            if attempt + 1 < attempts:
                time.sleep(2 ** attempt)
    raise last_error


def run(*, target: int, provider_id: str | None, limit: int, state_path: Path, pause_seconds: float, start_index: int = 0, end_index: int | None = None) -> int:
    all_characters = api_server.load_json(api_server.CHARACTERS_PATH)
    characters = select_character_range(all_characters, start_index, end_index)
    state = load_state(state_path)
    names = load_character_names()
    counts = count_names_for_character_set(names, all_characters)
    state.update({
        "target_per_character": target,
        "character_count": len(all_characters),
        "character_range": [start_index, end_index],
        "quality_status": "ai_pre_screened_not_human_reviewed",
        "counting_basis": "ai_complete_character_roles_and_naturalness_at_least_4",
        "last_updated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    })
    processed_this_run = 0
    while True:
        counts = count_names_for_character_set(load_character_names(), all_characters)
        if generation_complete(counts, all_characters, target):
            break
        phase, pending = phase_targets(characters, counts, target, all_characters)
        if limit and processed_this_run >= limit:
            break
        if not pending:
            if should_wait_for_other_ranges(counts, characters, all_characters, target):
                state["phase"] = "waiting_for_other_ranges"
                state["current_character"] = ""
                state["last_error"] = "本分片已达标，等待其他字区达到目标"
                state["last_updated"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
                save_state(state_path, state)
                time.sleep(30.0)
                continue
            break
        pending_by_character = dict(pending)
        queue = [character for character in state.get('cycle_remaining', [])
                 if character in pending_by_character]
        if not queue:
            queue = [character for character, _ in pending]
        state['cycle_remaining'] = queue
        pending = [(character, pending_by_character[character]) for character in queue]
        retry_after = state.setdefault('retry_after', {})
        pending = targets_for_cycle(pending, retry_after, now=time.time())
        if not pending:
            time.sleep(30.0)
            continue
        cycle_progress = False
        for character, _ in pending:
            if limit and processed_this_run >= limit:
                break
            character_target = target
            deficit = max(0, character_target - counts.get(character, 0))
            requested_for_character = deficit
            if deficit == 0:
                continue
            processed_this_run += 1
            state["phase"] = phase
            chunk_size = request_batch_size(phase, requested_for_character)
            state["current_character"] = character
            state["current_chunk"] = chunk_size
            save_state(state_path, state)
            try:
                result = request_with_retries({
                    "character": character,
                    "total_count": chunk_size,
                    "lengths": LENGTHS,
                    "providerId": provider_id,
                })
                before_count = counts.get(character, 0)
                names = load_character_names()
                counts = count_names_for_character_set(names, all_characters)
                actual_added = counts.get(character, 0) - before_count
                if actual_added > 0:
                    retry_after.pop(character, None)
                else:
                    retry_after[character] = time.time() + 300
                run = state.setdefault("runs", {}).setdefault(character, {
                    "existing_before": before_count,
                    "added": 0,
                    "requested": 0,
                    "rejected": 0,
                    "quality_status": "ai_pre_screened_not_human_reviewed",
                    "provider": result["providerName"],
                    "model": result["model"],
                })
                run["added"] = max(0, counts.get(character, 0) - run["existing_before"])
                run["requested"] += chunk_size
                run["rejected"] += result["rejected"]
                run["current_total"] = counts.get(character, 0)
                deficit = max(0, character_target - counts.get(character, 0))
                state["current_total"] = counts.get(character, 0)
                state["remaining_characters"] = sum(
                    1 for value in counts.values() if value == 0
                )
                state["remaining_deficit"] = sum(max(0, target - value) for value in counts.values())
                state["last_error"] = ""
                state["consecutive_zero_additions"] = 0 if actual_added else state.get("consecutive_zero_additions", 0) + 1
                state["last_updated"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
                state['cycle_remaining'].remove(character)
                save_state(state_path, state)
                print(json.dumps({
                    "character": character,
                    "total": counts.get(character, 0),
                    "added": actual_added,
                    "requested": chunk_size,
                    "error": "",
                }, ensure_ascii=False), flush=True)
            except Exception as error:  # preserve checkpoint and continue with the next character
                state["last_error"] = f"{character}: {type(error).__name__}: {error}"
                state.setdefault("errors", []).append(state["last_error"])
                retry_after[character] = time.time() + 300
                state["last_updated"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
                if character in state['cycle_remaining']:
                    state['cycle_remaining'].remove(character)
                save_state(state_path, state)
                continue
            cycle_progress = cycle_progress or actual_added > 0
            state["consecutive_zero_additions"] = 0
            state["processed_characters"] = state.get("processed_characters", 0) + 1
            state["current_character"] = ""
            state["last_updated"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            save_state(state_path, state)
            if pause_seconds:
                time.sleep(pause_seconds)
        if not cycle_progress:
            state["last_error"] = "本轮没有新增合格候选；等待后重试未覆盖字"
            state["last_updated"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            save_state(state_path, state)
            if should_retry_idle_cycle(counts, all_characters, target):
                time.sleep(max(30.0, pause_seconds))
                continue
            break
    final_counts = count_names_for_character_set(load_character_names(), all_characters)
    final_phase, remaining = phase_targets(characters, final_counts, target, all_characters)
    state["remaining_characters"] = sum(value < target for value in final_counts.values())
    state["remaining_deficit"] = sum(max(0, target - value) for value in final_counts.values())
    state["generation_target_reached"] = generation_complete(final_counts, all_characters, target)
    state["completed"] = False  # Generated/self-rated evidence cannot establish final quality acceptance.
    state["phase"] = "awaiting_quality_acceptance" if state["generation_target_reached"] else final_phase
    state["last_updated"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    save_state(state_path, state)
    print(json.dumps({
        "completed": state["completed"],
        "remaining_characters": state["remaining_characters"],
        "remaining_deficit": state["remaining_deficit"],
        "quality_status": state["quality_status"],
    }, ensure_ascii=False), flush=True)
    return 0 if state["completed"] else 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=int, default=TARGET_PER_CHARACTER)
    parser.add_argument("--provider-id", default=None)
    parser.add_argument("--limit", type=int, default=0, help="process at most this many characters; zero means all")
    parser.add_argument("--state", type=Path, default=STATE_PATH)
    parser.add_argument("--pause-seconds", type=float, default=0.0)
    parser.add_argument("--start-index", type=int, default=0, help="inclusive index in the full character list")
    parser.add_argument("--end-index", type=int, default=None, help="exclusive index in the full character list")
    arguments = parser.parse_args()
    raise SystemExit(run(
        target=arguments.target, provider_id=arguments.provider_id, limit=arguments.limit,
        state_path=arguments.state, pause_seconds=arguments.pause_seconds,
        start_index=arguments.start_index, end_index=arguments.end_index,
    ))
