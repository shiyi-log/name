#!/usr/bin/env python3
"""Independently review generated names in resumable batches, retaining evidence.

Model keeps remain provisional. Rejects and uncertain names leave generation
progress until a later review explicitly resolves them. No literature citation
is required. Generator explanations and self-ratings are withheld from review.
"""
from __future__ import annotations

import argparse
import collections
import functools
import hashlib
import json
import re
import sqlite3
import sys
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import api_server
from scripts.generate_all_character_candidates import load_character_names, count_names_for_character_set


def validate_reviews(names: list[str], payload: dict) -> list[dict]:
    reviews = payload.get('reviews')
    if not isinstance(reviews, list) or len(reviews) != len(names):
        raise ValueError('复核返回缺项或多项；整批不写入')
    seen = set()
    for item in reviews:
        if not isinstance(item, dict) or item.get('name') not in names or item['name'] in seen:
            raise ValueError('复核名称不匹配或重复；整批不写入')
        if item.get('decision') not in {'keep', 'reject', 'uncertain'}:
            raise ValueError('复核决定无效；整批不写入')
        if not isinstance(item.get('reason'), str) or not item['reason'].strip():
            raise ValueError('复核缺少理由；整批不写入')
        seen.add(item['name'])
    return reviews


def ensure_review_table(connection: sqlite3.Connection) -> None:
    connection.execute('''CREATE TABLE IF NOT EXISTS candidate_quality_reviews (
        name TEXT NOT NULL, review_id TEXT NOT NULL, model TEXT NOT NULL,
        decision TEXT NOT NULL, reason TEXT NOT NULL,
        reviewed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(name, review_id))''')


def save_reviews(path: Path, reviews: list[dict], review_id: str, model: str) -> None:
    with sqlite3.connect(path, timeout=30) as connection:
        ensure_review_table(connection)
        for item in reviews:
            connection.execute('''INSERT INTO candidate_quality_reviews
                (name,review_id,model,decision,reason) VALUES (?,?,?,?,?)''',
                (item['name'], review_id, model, item['decision'], item['reason']))
            if item['decision'] == 'keep':
                continue
            status = '质量复核未通过' if item['decision'] == 'reject' else '质量复核待定'
            connection.execute('''UPDATE name_candidates SET status=?,registry_note=?
                WHERE name=? AND status NOT IN ('质量复核未通过','质量复核待定')''',
                (status, '独立模型复核（非最终验收）：' + item['reason'], item['name']))


def review_progress(path: Path, records: list, characters: list[str], target: int) -> dict:
    with sqlite3.connect(path, timeout=30) as connection:
        ensure_review_table(connection)
        decisions = {}
        for name, decision in connection.execute('SELECT name,decision FROM candidate_quality_reviews ORDER BY rowid'):
            decisions[name] = decision
    kept = [record for record in records if decisions.get(record[0]) == 'keep']
    counts = count_names_for_character_set(kept, characters)
    return {
        'updated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        'quality_status': 'independent_model_screen_not_final_acceptance',
        'reviewed_names': len(decisions),
        'decision_counts': dict(collections.Counter(decisions.values())),
        'kept_name_count': len(kept),
        'unreviewed_eligible_names': sum(record[0] not in decisions for record in records),
        'independent_screen_counts': counts,
        'at_target': sum(value >= target for value in counts.values()),
        'remaining_deficit': sum(max(0, target-value) for value in counts.values()),
        'final_acceptance': False,
    }


def comparison_names(names: list[str], records: list, decisions: dict, per_character: int = 12) -> dict:
    targets = set(names)
    characters = set(''.join(names))
    related = {}
    for record in records:
        name = record[0]
        if name in targets or decisions.get(name) != 'keep':
            continue
        for character in sorted(set(name) & characters):
            group = related.setdefault(character, [])
            if name not in group:
                group.append(name)
    target_characters = [set(name) for name in names]
    return {character: sorted(group, key=lambda name: -max(
        len(set(name) & target) for target in target_characters))[:per_character]
        for character, group in related.items()}


def review_prompt(names: list[str], related_names: dict | None = None) -> dict:
    return {
        'task': '独立复核中文公司品牌名称。只依据完整名称本身判断，不看生成解释或自评分。',
        'rules': [
            '文献仅供参考，不要求出处；有依据的古语可以使用，不因其古典或不常见直接判错',
            'literature_phrase_examples 是本地书库的实际连续字形用例，需结合语境核义；命中不等于适合品牌，没有命中也不是否决理由。不要把仅因单字简释缺项而不认识的真实词语断言为机械拼接',
            '锚定字可以生僻；不得只因生僻字否决，但需核对真实字义，禁止借部件、同音字或形近字的含义',
            'direct_dictionary 的 definition 是原字直接释义；related_characters 明列繁体关联字与其各自释义，不能视为原字所有义项。supplemental_dictionary 是有来源的第三方补充字典，须结合词义及语境交叉核对，不可因缺少顶层 definition 就无视已提供的补充资料',
            '字典简释并非穷尽义项，也不是逐字必须命中的白名单。普通字的常识性义项、构词后缀（如“然”的状态后缀）不因简释未列出就判无法核义；核查完整词语和语境，实际无法确认或存在冲突时才uncertain',
            'official_dictionary 保留教育部修订本中同形字的原始释义及读音，各读音分别列出；只按实际词义和语境使用，不能将多音义项混用。其字形为繁体原文，不要求名称必须在该字典命中',
            'dictionary_phrase_entries 是教育部修订本中与完整名称同形的原始词条及读音；用于核对整词本义，词条命中不等于适合品牌，没有命中也不是否决理由',
            '公司行业、地域和创始人背景均未知，不得擅自假设地域根基或特定行业来替名称解释',
            'reject：明显机械堆字、语法生硬、句子/口号、通用功能类别词，或字义错误、谐音借义',
            '构词通顺只是必要条件。生成要求已排除常见成语和空泛口号，例如“一鼓作气”“一视同仁”；不得仅因是熟悉成语或价值宣言就keep。完整名称必须具备品牌名的凝练感，不能把完整判断句当名称',
            'uncertain：实际字义仍无法确认、专名关系缺乏依据或自然度存疑；不得为了数量放宽',
            'keep：完整组合自然，有清楚而不牵强的语义和品牌辨识度；普通自然词语也可能适合品牌，不能一律拒绝',
            '行业未知不是否决理由。自然物象、简洁动作词、抽象词可以成为品牌；只有名称完整地等同于通用产品或服务类别，或整名实际生硬、借义、口号化，才按这些问题否决。不要把“描述某物”“不是固定词”“没有独特寓意”本身作为reject理由，也不要只因锚字生僻就uncertain',
            '检查批内换序或只靠同义字替换的近似组合，重复组最多保留一个',
            'related_kept_candidates 是其他批次暂保留的同字候选，仅用于跨批比较。只有实际相同意象与关系、仅换近义配字的组合才视为重复；共享一个字或构词结构本身不代表重复。新名仅为旧名近似变体时不再keep，理由写明对照名称；不要为对照名称返回决定',
            '返回每一个输入名称及具体理由。keep仅代表继续待复核，不代表完成验收或工商商标可用',
        ],
        'names': names,
        'related_kept_candidates': related_names or {},
        'literature_phrase_examples': phrase_references(names),
        'dictionary_phrase_entries': dictionary_phrase_references(names),
        'direct_dictionary': {ch: api_server.character_definition_context(ch) for name in names for ch in name},
        'response_format': {'reviews': [{'name': '输入原名', 'decision': 'keep | reject | uncertain', 'reason': '具体判断理由'}]},
    }


@functools.lru_cache(maxsize=2)
def _phrase_dictionary(path: Path) -> dict:
    if not path.exists():
        return {}
    entries = {}
    for record in json.loads(path.read_text(encoding='utf-8')):
        entries.setdefault(record['title'], []).append(record)
    return entries


def dictionary_phrase_references(names: list[str], path: Path | None = None) -> dict:
    if path is None:
        path = ROOT / '参考文献' / '字义数据' / 'moe-revised-2015_20260929' / 'entries.json'
    entries = _phrase_dictionary(path)
    source_path = path.with_name('source.json')
    source = json.loads(source_path.read_text()) if source_path.exists() else {}
    return {name: [{**record, 'source_url': source.get('source_url'),
                    'source_sha256': source.get('sha256')} for record in entries.get(name, [])]
            for name in names}


def phrase_references(names: list[str], corpus: dict[str, str] | None = None) -> dict:
    contexts = {name: [] for name in names}
    if not names:
        return contexts
    pattern = re.compile('|'.join(re.escape(name) for name in sorted(names, key=len, reverse=True)))
    if corpus is None:
        books = json.loads(api_server.LITERATURE_INDEX_PATH.read_text(encoding='utf-8'))
        sources = ((book['title'], api_server.LITERATURE_INDEX_PATH.parent /
                    (book.get('simplified_path') or book['text_path'])) for book in books)
    else:
        sources = corpus.items()
    for book, source in sources:
        text = source if isinstance(source, str) else source.read_text(encoding='utf-8')
        for line_number, line in enumerate(text.splitlines(), 1):
            if not pattern.search(line):
                continue
            for sentence in re.split(r'(?<=[。！？；])', line):
                for name in names:
                    position = sentence.find(name)
                    if position < 0:
                        continue
                    if len(contexts[name]) >= 2:
                        continue
                    contexts[name].append({'book': book, 'line': line_number,
                        'context': sentence[max(0,position-45):position+len(name)+65].strip()})
        if all(len(items) >= 2 for items in contexts.values()):
            break
    return contexts


def request_review(names: list[str], provider: dict, related_names: dict | None = None) -> tuple[list[dict], dict]:
    prompt = review_prompt(names, related_names)
    system = '你是严格的中文品牌名复核者，独立于生成过程。仅返回JSON对象，不附Markdown。'
    if provider['wireApi'] == 'responses':
        body = {'model': provider['model'], 'input': [
            {'role': 'system', 'content': [{'type': 'input_text', 'text': system}]},
            {'role': 'user', 'content': [{'type': 'input_text', 'text': json.dumps(prompt, ensure_ascii=False)}]}
        ], 'text': {'format': {'type': 'json_object'}}, 'stream': True}
    else:
        body = {'model': provider['model'], 'messages': [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': json.dumps(prompt, ensure_ascii=False)}
        ], 'response_format': {'type': 'json_object'}}
    request = urllib.request.Request(api_server._response_endpoint(provider['baseUrl'], provider['wireApi']),
        data=json.dumps(body).encode(), headers={'Authorization': 'Bearer ' + provider['_api_key'],
        'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(request, timeout=300) as response:
        result = (api_server._read_responses_stream(response) if provider['wireApi'] == 'responses'
                  else json.load(response))
    payload = api_server._json_from_ai_text(api_server._response_text(result, provider['wireApi']))
    return validate_reviews(names, payload), prompt


def select_review_names(records: list, reviewed: set, batch_size: int, shard_index: int, shard_count: int) -> list[str]:
    if shard_count < 1 or not 0 <= shard_index < shard_count:
        raise ValueError('invalid review shard range')
    return [record[0] for record in records if record[0] not in reviewed
            and int.from_bytes(hashlib.sha256(record[0].encode()).digest()[:8], 'big') % shard_count == shard_index][:batch_size]


def run(batch_size: int, max_batches: int, provider_id: str | None, shard_index: int = 0, shard_count: int = 1) -> None:
    provider = api_server.selected_ai_provider(provider_id)
    if provider is None:
        raise RuntimeError('没有可用复核服务商')
    output = ROOT / 'data' / 'quality-reviews' / 'batches'
    output.mkdir(parents=True, exist_ok=True)
    processed = 0
    while not max_batches or processed < max_batches:
        with sqlite3.connect(api_server.DB_PATH, timeout=30) as connection:
            ensure_review_table(connection)
            decisions = dict(connection.execute('SELECT name,decision FROM candidate_quality_reviews ORDER BY rowid'))
        records = load_character_names()
        names = select_review_names(records, set(decisions), batch_size, shard_index, shard_count)
        if not names:
            if max_batches:
                return
            time.sleep(30)
            continue
        try:
            decisions, prompt = request_review(names, provider, comparison_names(names, records, decisions))
            review_id = time.strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8]
            evidence = {'review_id': review_id, 'model': provider['model'],
                'shard_index': shard_index, 'shard_count': shard_count,
                'quality_status': 'independent_model_screen_not_final_acceptance',
                'input': prompt, 'reviews': decisions}
            path = output / (review_id + '.json')
            path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n')
            save_reviews(api_server.DB_PATH, decisions, review_id, provider['model'])
            progress = review_progress(api_server.DB_PATH, load_character_names(),
                api_server.load_json(api_server.CHARACTERS_PATH), 500)
            progress_filename = ('character_quality_review_progress.json' if shard_count == 1
                                 else f'character_quality_review_progress_{shard_index}.json')
            progress_path = ROOT / 'data' / progress_filename
            progress_path.with_suffix('.tmp').write_text(json.dumps(progress, ensure_ascii=False, indent=2))
            progress_path.with_suffix('.tmp').replace(progress_path)
            processed += 1
            print(json.dumps({'review_id': review_id, 'counts': dict(collections.Counter(item['decision'] for item in decisions))}), flush=True)
        except (OSError, RuntimeError, ValueError) as error:
            print(json.dumps({'error': str(error), 'batch_not_applied': names}, ensure_ascii=False), flush=True)
            time.sleep(15)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--batch-size', type=int, default=24)
    parser.add_argument('--max-batches', type=int, default=0)
    parser.add_argument('--provider-id', default=None)
    parser.add_argument('--shard-index', type=int, default=0)
    parser.add_argument('--shard-count', type=int, default=1)
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 48 or args.max_batches < 0:
        parser.error('batch-size must be 1..48; max-batches must be nonnegative')
    if args.shard_count < 1 or not 0 <= args.shard_index < args.shard_count:
        parser.error('invalid review shard range')
    run(args.batch_size, args.max_batches, args.provider_id, args.shard_index, args.shard_count)
