#!/usr/bin/env python3
"""Audit all character deficits from a consistent, read-only source snapshot."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import api_server
from scripts import generate_all_character_candidates as generation
from scripts import review_character_candidates as review


def build_audit(database: Path, characters: list[str], target: int) -> dict:
    # SQLite backup captures one coherent view while generators keep writing.
    # Review table initialization happens only in the disposable snapshot.
    with tempfile.TemporaryDirectory(prefix='name-audit-') as directory:
        snapshot = Path(directory) / 'snapshot.db'
        with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=30) as source:
            with sqlite3.connect(snapshot) as destination:
                source.backup(destination)
        original = api_server.DB_PATH
        try:
            api_server.DB_PATH = snapshot
            records = generation.load_character_names()
            counts = generation.count_names_for_character_set(records, characters)
            progress = review.review_progress(snapshot, records, characters, target)
        finally:
            api_server.DB_PATH = original
        screen = progress.pop('independent_screen_counts')
        with sqlite3.connect(snapshot) as connection:
            provisional = generation.count_names_for_characters([name for name, in connection.execute(
                "SELECT name FROM name_candidates WHERE name_length BETWEEN 2 AND 4 "
                "AND status NOT IN ('质量复核未通过','质量复核待定')")])
            statuses = dict(connection.execute('SELECT status,COUNT(*) FROM name_candidates GROUP BY status'))
            check = connection.execute('PRAGMA quick_check').fetchone()[0]
    return {
        'generated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        'target_per_character': target, 'character_count': len(characters),
        'database_quick_check': check,
        'quality_status': 'provisional_counts_not_final_quality_acceptance',
        'counting_notes': [
            'provisional_count includes legacy candidates without individual semantic review',
            'provisional_count excludes rejected and uncertain rows',
            'ai_evidence_count excludes rejected and uncertain rows, requires roles for every name character and naturalness >=4',
            'ai_evidence_count counts distinct full names; order variants remain provisional until independent quality review',
            'ai_evidence_count is generator scheduling progress, not final quality acceptance',
            'literature is reference only, never a required provenance gate',
            'all counts use one SQLite snapshot; the source database is not modified',
        ],
        'summary': {
            'provisional_covered': sum(provisional.get(ch, 0) > 0 for ch in characters),
            'provisional_at_target': sum(provisional.get(ch, 0) >= target for ch in characters),
            'provisional_deficit': sum(max(0, target - provisional.get(ch, 0)) for ch in characters),
            'ai_evidence_covered': sum(value > 0 for value in counts.values()),
            'ai_evidence_at_target': sum(value >= target for value in counts.values()),
            'ai_evidence_deficit': sum(max(0, target - value) for value in counts.values()),
            'unique_ai_evidence_names': len(records),
            'quality_review_rejected': statuses.get('质量复核未通过', 0),
            'quality_review_uncertain': statuses.get('质量复核待定', 0),
            'independent_review': progress,
        },
        'characters': [{
            'character': ch, 'rank': index + 1,
            'provisional_count': provisional.get(ch, 0), 'ai_evidence_count': counts[ch],
            'independent_screen_count': screen[ch],
            'ai_evidence_deficit': max(0, target - counts[ch]),
            'independent_screen_deficit': max(0, target - screen[ch]),
            'final_quality_acceptance': 'unverified',
        } for index, ch in enumerate(characters)],
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=api_server.DB_PATH)
    parser.add_argument('--characters', type=Path, default=api_server.CHARACTERS_PATH)
    parser.add_argument('--output', type=Path, default=ROOT / 'data/character_generation_audit.json')
    parser.add_argument('--target', type=int, default=500)
    args = parser.parse_args()
    if args.target < 1:
        parser.error('target must be positive')
    audit = build_audit(args.database, json.loads(args.characters.read_text()), args.target)
    if args.output.exists():
        previous = json.loads(args.output.read_text())
        audit['counting_notes'] = list(dict.fromkeys(previous.get('counting_notes', []) + audit['counting_notes']))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + '.tmp')
    temporary.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + chr(10))
    temporary.replace(args.output)
    print(json.dumps({'generated_at': audit['generated_at'], 'summary': audit['summary'],
                      'database_quick_check': audit['database_quick_check']}, ensure_ascii=False))
