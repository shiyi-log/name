#!/usr/bin/env python3
"""Merge directly written proposal files; never generate names or accept quality."""
from pathlib import Path
from collections import Counter
import datetime
import hashlib
import json
import sqlite3

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'data/generation-pool'
AUDIT = OUT / 'baseline-audit-20261116.json'

def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()

def write(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + chr(10))
    temporary.replace(path)

def main():
    OUT.mkdir(parents=True, exist_ok=True)
    characters = json.loads((ROOT / 'data/characters-8105.json').read_text())
    library = set(characters)
    audit = json.loads(AUDIT.read_text())
    assert len(characters) == len(library) == 8105
    assert audit['character_count'] == 8105
    with sqlite3.connect((ROOT / 'data/names.db').as_uri() + '?mode=ro', uri=True) as connection:
        connection.execute('BEGIN')
        database_names = {row[0] for row in connection.execute('SELECT name FROM name_candidates')}
        database_count = connection.execute('SELECT COUNT(*) FROM name_candidates').fetchone()[0]
        quick_check = connection.execute('PRAGMA quick_check').fetchone()[0]
    snapshot = json.loads((ROOT / 'data/candidates.json').read_text())
    if isinstance(snapshot, dict):
        snapshot = snapshot.get('candidates', snapshot.get('names', []))
    snapshot_names = {row['name'] for row in snapshot}
    assert database_names == snapshot_names, 'database/snapshot name drift; rerun source audit'
    assert quick_check == 'ok'
    sources = sorted((ROOT / 'data/quality-reviews/direct-20261116-r85').glob('*proposals.json'))
    sources += sorted((ROOT / 'data/quality-reviews').glob('direct-20261116-r8[6789]/*generated.json'))
    # Existing evidence is retained as a scheduling caution, not a new review round.
    close = {
        '纸上桥': ('纸上小桥', '已有跨批裁决指出缩写近似'),
        '匣中月': ('柜中月', '已有跨批裁决指出同一容器藏月关系'),
        '木叶间': ('柚叶间', '已有跨批裁决指出叶间画面的泛化'),
        '翻册': ('翻书页', '已有单份初筛指出翻阅动作近似，留待统一裁决'),
        '本来': ('原本', '已有单份初筛指出原先义近似，留待统一裁决'),
        '本性': ('天性', '已有单份初筛指出先天性情义近似，留待统一裁决'),
    }
    entries = []
    index = {}
    source_index = []
    occurrences = 0
    for source in sources:
        doc = json.loads(source.read_text())
        proposals = doc['proposals']
        relative = str(source.relative_to(ROOT))
        source_hash = sha(source)
        source_index.append({'path': relative, 'sha256': source_hash, 'proposal_count': len(proposals)})
        for position, proposal in enumerate(proposals):
            occurrences += 1
            name = proposal['name']
            roles = proposal['character_roles']
            assert 2 <= len(name) <= 4 and set(name) <= library, (relative, name)
            assert proposal['anchor'] in name, (relative, name, 'anchor')
            assert set(roles) == set(name), (relative, name, 'role coverage')
            assert all(isinstance(role, str) and role.strip() for role in roles.values())
            assert proposal['naturalness'] in (4, 5), (relative, name, 'naturalness')
            assert not proposal.get('counted_as_final', False)
            assert not proposal.get('counted', False)
            source_ref = {'path': relative, 'sha256': source_hash, 'json_pointer': '/proposals/' + str(position)}
            if name in index:
                index[name]['source_occurrences'].append(source_ref)
                continue
            reasons = []
            if name in database_names:
                reasons.append('existing_database_exact')
            if name[::-1] in database_names:
                reasons.append('existing_database_reverse')
            if name[::-1] in index:
                reasons.append('deferred_pool_reverse')
            if name in close:
                reasons.append('known_close_variant_defer_credit')
            entry = {
                'name': name, 'anchor': proposal['anchor'],
                'meaning': proposal['meaning'], 'character_roles': roles,
                'naturalness_self_assessment': proposal['naturalness'],
                'review_status': 'deferred_full_review', 'counted_as_final': False,
                'generation_credit_provisional': not reasons,
                'no_generation_credit_reasons': reasons,
                'source_occurrences': [source_ref],
                'risk_note': proposal.get('risk_note', proposal.get('review_note', '')),
            }
            if name in close:
                entry['existing_review_caution'] = {'comparison': close[name][0], 'note': close[name][1]}
            entries.append(entry)
            index[name] = entry
    credit = [entry for entry in entries if entry['generation_credit_provisional']]
    coverage = Counter(character for entry in credit for character in set(entry['name']))
    anchor_counts = Counter(entry['anchor'] for entry in credit)
    rows = []
    for original in audit['characters']:
        character = original['character']
        existing = original['ai_evidence_count']
        pending = coverage[character]
        rows.append({
            'character': character, 'rank': original['rank'], 'target': 500,
            'existing_ai_evidence_count': existing,
            'new_deferred_generation_count': pending,
            'generation_scheduling_count': existing + pending,
            'generation_deficit': max(0, 500 - existing - pending),
            'independent_screen_count_before_generation': original['independent_screen_count'],
            'final_quality_acceptance': 'unverified',
        })
    now = datetime.datetime.now().astimezone().isoformat()
    metadata = {
        'updated_at': now, 'scope': 'all_8105_characters_500_each',
        'stage': 'generation_first_then_unified_review', 'final_acceptance': False,
        'accepted_count_from_this_pool': 0,
        'literature_policy': 'reference_only_not_required_provenance',
        'generation_method': 'assistant_and_subagents_direct_composition_no_project_model_api',
        'source_proposal_occurrences': occurrences, 'unique_pool_names': len(entries),
        'provisional_new_names_with_generation_credit': len(credit),
        'withheld_generation_credit_names': len(entries) - len(credit),
        'duplicate_source_occurrences': occurrences - len(entries),
        'counts_by_anchor': dict(sorted(anchor_counts.items())),
        'characters_with_new_deferred_candidates': len(coverage),
        'characters_at_generation_target_provisional': sum(row['generation_scheduling_count'] >= 500 for row in rows),
        'remaining_generation_deficit_provisional': sum(row['generation_deficit'] for row in rows),
        'baseline_audit_generated_at': audit['generated_at'],
        'baseline_audit_sha256': sha(AUDIT),
        'baseline_unique_ai_evidence_names': audit['summary']['unique_ai_evidence_names'],
        'database_name_candidate_rows': database_count, 'database_quick_check': quick_check,
        'database_snapshot_name_sets_equal': True,
        'candidate_snapshot_sha256': sha(ROOT / 'data/candidates.json'),
        'source_files': source_index,
        'counting_notes': [
            'All new entries remain deferred, with zero final acceptance credit.',
            'Scheduling counts combine preexisting AI-evidence rows with distinct new deferred names; these are not accepted quality counts.',
            'A name contributes once to every distinct library character with a stated semantic role; repeated characters do not multiply credit.',
            'Existing exact/reverse names and known close variants receive no additional generation credit.',
            'The complete historical semantic and exact-duplicate check is deferred to unified review; current R86 basic check is not final deduplication.',
            'Ordinary imagery, actions and abstract concepts are allowed; industry and literary provenance are not admission requirements.',
            'No Cartesian-product generation or model endpoint is called by this merge program.',
        ],
    }
    temporary = OUT / 'candidates.jsonl.tmp'
    temporary.write_text(''.join(json.dumps(entry, ensure_ascii=False) + chr(10) for entry in entries))
    temporary.replace(OUT / 'candidates.jsonl')
    write(OUT / 'progress.json', {**metadata, 'characters': rows})
    write(OUT / 'manifest.json', {**metadata, 'pool_sha256': sha(OUT / 'candidates.jsonl'), 'progress_sha256': sha(OUT / 'progress.json')})
    print(json.dumps({key: value for key, value in metadata.items() if key not in ('source_files', 'counting_notes', 'counts_by_anchor')}, ensure_ascii=False))

if __name__ == '__main__':
    main()
