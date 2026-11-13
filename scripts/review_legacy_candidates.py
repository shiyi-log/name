#!/usr/bin/env python3
"""Prepare resumable semantic and independent reviews of legacy AI names.

Writes evidence only. No model decision or self-rating is applied to the DB.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import sqlite3
import sys
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import api_server
from scripts.generate_all_character_candidates import load_character_names, parse_character_roles
from scripts.review_character_candidates import (comparison_names, dictionary_phrase_references,
                                                  phrase_references, request_review)


def select_legacy_names(database, prepared, batch_size):
    with sqlite3.connect(f'file:{Path(database).resolve()}?mode=ro', uri=True) as connection:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        reviewed = ({row[0] for row in connection.execute('SELECT name FROM candidate_quality_reviews')}
                    if 'candidate_quality_reviews' in tables else set())
        result = []
        for name, notes in connection.execute(
                "SELECT name,notes_json FROM name_candidates WHERE source_type='ai' "
                "AND name_length BETWEEN 2 AND 4 "
                "AND status NOT IN ('质量复核未通过','质量复核待定') ORDER BY name_length,rowid"):
            if name in prepared or name in reviewed or parse_character_roles(notes):
                continue
            result.append(name)
            if len(result) >= batch_size:
                break
        return result


def validate_semantics(names, payload):
    items = payload.get('names')
    if not isinstance(items, list) or len(items) != len(names):
        raise ValueError('语义核查缺项或多项')
    seen = set()
    for item in items:
        if not isinstance(item, dict) or item.get('name') not in names or item['name'] in seen:
            raise ValueError('语义核查名称不匹配或重复')
        seen.add(item['name'])
        roles = item.get('character_roles')
        if not isinstance(roles, dict) or not set(item['name']).issubset(roles):
            raise ValueError('语义角色缺项')
        if any(not isinstance(roles[ch], str) or not roles[ch].strip() for ch in set(item['name'])):
            raise ValueError('语义角色无效')
        if not isinstance(item.get('meaning'), str) or not item['meaning'].strip():
            raise ValueError('整词释义缺失')
        rating = item.get('naturalness')
        if type(rating) not in (int, float) or not 1 <= rating <= 5:
            raise ValueError('自然度评分无效')
    return items


def prepared_names(directory):
    names = set()
    for path in Path(directory).glob('*.json'):
        try:
            data = json.loads(path.read_text())
            if data.get('status') == 'legacy_review_not_applied':
                names.update(data['names'])
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return names


def request_semantics(names, provider):
    prompt = {
        'task': '核查名称整词本义、各字具体构词作用及自然度，不为通过而补造意义。',
        'names': names,
        'rules': [
            '文献只供参考，不要求出处；检索命中不等于成立，未命中不等于不成立。',
            '先理解整词再说明各字作用，不得借部件、形似或同音字的意义。',
            '每字需说明实际构词作用，仅作核心识别字、姓氏式识别字或古雅专名感不足。',
            '自然新组合不要求是固定词，普通自然词语不因行业未知或常见而否决。',
            'naturalness为1到5，实际不确定或不自然则给低分，禁止为数量提高评分。',
            '仅返回JSON names列表，每项name、meaning、character_roles、naturalness。',
        ],
        'dictionary': {ch: api_server.character_definition_context(ch) for name in names for ch in name},
        'dictionary_phrases': dictionary_phrase_references(names),
        'literature_examples': phrase_references(names),
    }
    system = '你是严谨的中文语义核查者，仅返回JSON对象。'
    if provider['wireApi'] == 'responses':
        body = {'model': provider['model'], 'input': [
            {'role': 'system', 'content': [{'type': 'input_text', 'text': system}]},
            {'role': 'user', 'content': [{'type': 'input_text', 'text': json.dumps(prompt, ensure_ascii=False)}]},
        ], 'text': {'format': {'type': 'json_object'}}}
    else:
        body = {'model': provider['model'], 'messages': [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': json.dumps(prompt, ensure_ascii=False)},
        ], 'response_format': {'type': 'json_object'}}
    request = urllib.request.Request(
        api_server._response_endpoint(provider['baseUrl'], provider['wireApi']),
        data=json.dumps(body).encode(), headers={
            'Authorization': 'Bearer ' + provider['_api_key'], 'Content-Type': 'application/json'},
        method='POST')
    with urllib.request.urlopen(request, timeout=90) as response:
        result = json.load(response)
    payload = api_server._json_from_ai_text(api_server._response_text(result, provider['wireApi']))
    try:
        return validate_semantics(names, payload), prompt
    except ValueError as error:
        error.semantic_payload = payload
        error.semantic_input = prompt
        raise


def run(database, output, batch_size, max_batches, provider_id):
    provider = api_server.selected_ai_provider(provider_id)
    if provider is None:
        raise RuntimeError('没有可用AI服务商')
    output.mkdir(parents=True, exist_ok=True)
    prepared = prepared_names(output)
    for _ in range(max_batches):
        names = select_legacy_names(database, prepared, batch_size)
        if not names:
            break
        with sqlite3.connect(database) as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            decisions = (dict(connection.execute('SELECT name,decision FROM candidate_quality_reviews ORDER BY rowid'))
                         if 'candidate_quality_reviews' in tables else {})
        # Review sees names and references, never the semantic self-rating.
        results, errors, invalid_payloads = {}, {}, {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            semantic_job = pool.submit(request_semantics, names, provider)
            review_job = pool.submit(request_review, names, provider,
                comparison_names(names, load_character_names(), decisions, per_character=30))
            for kind, job in [('semantics', semantic_job), ('reviews', review_job)]:
                try:
                    results[kind] = job.result()
                except (OSError, RuntimeError, ValueError) as error:
                    errors[kind] = f'{type(error).__name__}: {error}'
                    if hasattr(error, 'semantic_payload'):
                        invalid_payloads[kind] = {
                            'input': error.semantic_input, 'output': error.semantic_payload}
        semantics, semantic_prompt = results.get('semantics', ([], {}))
        reviews, review_prompt = results.get('reviews', ([], {}))
        evidence = {
            'created_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
            'status': 'legacy_review_failed' if errors else 'legacy_review_not_applied',
            'final_acceptance': False, 'errors': errors, 'invalid_payloads': invalid_payloads,
            'names': names, 'semantics': semantics, 'reviews': reviews,
            'semantic_input': semantic_prompt, 'review_input': review_prompt,
            'model': provider['model'],
        }
        path = output / (time.strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8] + '.json')
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + chr(10))
        temporary.replace(path)
        print(json.dumps({'evidence': str(path), 'names': len(names),
                          'applied': False, 'errors': errors}), flush=True)
        if errors:
            break
        prepared.update(names)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=api_server.DB_PATH)
    parser.add_argument('--output', type=Path, default=ROOT / 'data/quality-reviews/legacy-batches')
    parser.add_argument('--batch-size', type=int, default=24)
    parser.add_argument('--max-batches', type=int, default=1)
    parser.add_argument('--provider-id', default=None)
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 48 or args.max_batches < 1:
        parser.error('batch-size must be 1..48; max-batches must be positive')
    run(args.database, args.output, args.batch_size, args.max_batches, args.provider_id)
