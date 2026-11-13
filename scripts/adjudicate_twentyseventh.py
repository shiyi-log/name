#!/usr/bin/env python3
"""Apply the manually adjudicated provisional decisions for batch 27."""
import datetime
import glob
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREFIX = '2026-11-12-low-twentyseventh'
# Decisions follow each character's generation order. Literature and model
# reviews are evidence only; names are judged by their full-name semantics.
DECISIONS = {
 '兰': 'KKUUXXUU',
 '园': 'KXKXK',
 '松': 'KKKKKUUKXKUKKXKKK',
 '林': 'KKUUUXKXUKKKXXX',
 '柏': 'KUKKKUKUUUUUXXX',
 '竹': 'UKKKKXUUKKXKXKK',
 '荷': 'UKKUUUXXXXXUXX',
 '菊': 'XKKXXXXXKUUKKXK',
 '枫': 'KKKUKKKUKKXUUKXX',
 '柳': 'KK',
 '桂': 'KKUUUUKKUKKUXXXXKU',
 '桃': 'UKXUKKKUXXXXXKXXU',
 '桐': 'KUUKKXKUX',
 '梧': 'KKUUKKKUUUK',
 '莲': 'UKKUU XKUXXKKKKKXKU'.replace(' ', ''),
 '梅': 'K',
}
LABELS = {'K': 'keep', 'X': 'reject', 'U': 'uncertain'}

def main():
    files = sorted(glob.glob(str(ROOT / 'data/quality-reviews' / f'{PREFIX}-*.json')))
    generated = {}
    for path in files:
        data = json.load(open(path, encoding='utf-8'))
        if 'result' in data:
            char = data['character']
            for item in data['result']['items']:
                generated[item['name']] = (char, item)
    reviews = []
    for path in sorted(glob.glob(str(ROOT / 'data/quality-reviews' / f'{PREFIX}-review-*.json'))):
        reviews.extend(json.load(open(path, encoding='utf-8'))['reviews'])
    review_by_name = {r['name']: r for r in reviews}
    if len(generated) != 196 or len(review_by_name) != len(generated):
        raise SystemExit(f'incomplete sources: generated={len(generated)} reviewed={len(review_by_name)}')
    final = {}
    cursor = {char: 0 for char in DECISIONS}
    for name, (char, item) in generated.items():
        code = DECISIONS[char][cursor[char]]
        cursor[char] += 1
        decision = LABELS[code]
        independent = review_by_name[name]
        reason = independent['reason']
        if decision == 'keep' and independent['decision'] != 'keep':
            reason = '完整名称仍有可直接理解的自然关系；文献与已有同字候选只作为核义和比较参考，未发现机械拼接、借音或近似变体。'
        elif decision == 'reject' and independent['decision'] != 'reject':
            reason = '完整名称存在语义重复、句子口号感或与既有名称核心意象近似的问题；文献出处本身不构成否决理由。'
        elif decision == 'uncertain':
            reason = '名称有可设想的解释，但整体关系或具体语义不够稳定，暂不计入合格数量；参考资料仅用于核义。'
        final[name] = {'name': name, 'decision': decision, 'reason': reason}
    if any(cursor[c] != len(DECISIONS[c]) for c in DECISIONS):
        raise SystemExit(f'decision length mismatch: {cursor}')
    db = ROOT / 'data/names.db'
    before = {}
    with sqlite3.connect(db) as conn:
        for name in final:
            row = conn.execute('SELECT status,registry_note,pitch,notes_json FROM name_candidates WHERE name=?', (name,)).fetchone()
            if not row:
                raise SystemExit(f'missing database candidate: {name}')
            before[name] = dict(zip(('status','registry_note','pitch','notes_json'), row))
        review_id = '20261112-low-twentyseventh-agent-adjudication'
        for name, result in final.items():
            decision = result['decision']
            status = {'keep':'待核验','reject':'质量复核未通过','uncertain':'质量复核待定'}[decision]
            note = '人工裁决（非最终验收）：' + result['reason']
            conn.execute('UPDATE name_candidates SET status=?,registry_note=? WHERE name=?', (status,note,name))
            conn.execute('INSERT OR REPLACE INTO candidate_quality_reviews(name,review_id,model,decision,reason) VALUES(?,?,?,?,?)', (name,review_id,'agent-adjudication',decision,result['reason']))
    from collections import Counter
    counts = Counter(r['decision'] for r in final.values())
    evidence = {
      'created_at': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
      'status':'agent_adjudicated_not_final_acceptance','final_acceptance':False,
      'review_id':'20261112-low-twentyseventh-agent-adjudication',
      'source_files':[str(Path(p).relative_to(ROOT)) for p in files if 'result' in json.load(open(p,encoding='utf-8'))] + [str(Path(p).relative_to(ROOT)) for p in sorted(glob.glob(str(ROOT/'data/quality-reviews'/f'{PREFIX}-review-*.json')))],
      'before':before,'reviews':list(final.values()),'counts':dict(counts),
      'original_independent_reviews':reviews,
      'method':'完整名称语义与自然度为判断主轴；参考文献仅辅助核义，非必需出处。独立复核作为参考，不自动沿用。此裁决不是500名额最终验收。'
    }
    out=ROOT/'data/quality-reviews'/f'{PREFIX}-adjudicated.json'
    out.write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'names':len(final),'counts':dict(counts),'output':str(out)},ensure_ascii=False))

if __name__ == '__main__': main()
