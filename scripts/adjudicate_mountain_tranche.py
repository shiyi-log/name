#!/usr/bin/env python3
"""Record a manually screened tranche of 山 candidates with source evidence."""
import datetime
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DECISIONS = [
 ('暮山晴雪','keep','暮色山峦与晴后积雪构成完整、易感知的景象，四字节奏清楚；不依赖额外行业解释。'),
 ('山影浮','keep','山影浮于水面的省略式景象自然；本地宋词、宋诗均见连续字形用例，语境与倒影意象相合。'),
 ('山影落塘','reject','与已有暂留候选“山影入塘”表达相同的山影进入池塘关系，只替换近义动作字，属于近似变体。'),
 ('晚山叠翠','uncertain','可理解为暮色中层山青翠相叠，但整体接近现成景物描写，独立品牌辨识度尚不明确。'),
 ('秋山带霜','keep','秋日山峦覆霜的物候关系清楚，语序自然，形成完整而凝练的季节景象。'),
 ('冬雪晴山','keep','冬雪消停后晴山显现，景物与时序关系明确；组合简洁，不需借助特定出处。'),
 ('雾染秋山','keep','雾气笼染秋山的主谓关系自然，季节、气象和山色构成统一画面。'),
 ('晓山凝霭','keep','晨晓山峦与凝聚云霭构成连贯景象；“霭”在此承担真实云气义，未借形借音。'),
 ('云覆山','keep','“云覆山”是自然的主谓景象，意为云层覆盖山体；宋诗连续用例的语境相符，可核验其词面关系。'),
 ('篱外山','keep','可理解为篱笆之外的山景，处所关系明确；本地宋诗及《红楼梦》上下文均为篱外远山语境。'),
 ('晚山含黛','keep','以“含黛”形容暮色山峦的青黑色泽，修饰关系连贯，形成凝练且有色彩辨识度的景象。'),
 ('山含璧','keep','“山含璧”可理解为山中蕴藏美玉，“含”与山体藏玉的关系自然，寓意收敛，不是硬拼吉祥字。'),
 ('山映赭霞','keep','山体映着赭色云霞，光色关系清楚，四字意象统一且有鲜明色彩。'),
 ('山翠','uncertain','可指青翠山色，但独立完整名称容易退化为泛化景色属性，辨识度偏弱，暂不计数。'),
 ('山澈','reject','“澈”表示水清，生成解释借用了清泉意象；名称本身没有泉、水等语义成分承接，组合关系不成立。'),
 ('山宿','uncertain','可解作山中住宿或夜宿山间，但也直接像住宿服务类别，行业未知时品牌指向不足。'),
 ('山馆','uncertain','“山中之馆”语义完整自然，但整体偏通用场所类别，当前缺少足够独立辨识度。'),
 ('山影入塘','keep','山影进入池塘水面是自然的倒影关系，语序清楚；与“山影落塘”同组，仅保留这一版本。'),
 ('枕山','keep','“枕山”可表示居处枕倚山势，唐诗连续用例也处于临山居所语境；二字简洁，词义可核。'),
 ('蓬山路','uncertain','“蓬山”有蓬莱仙山的传统语义，文献中“蓬山路”用于仙境阻隔语境；意象真实，但固定典故与道路式表达的品牌辨识度待核。'),
]

def main():
    db = ROOT / 'data/names.db'
    before = {}
    with sqlite3.connect(db, timeout=30) as conn:
        original_reviews = {}
        for name, decision, reason in DECISIONS:
            row = conn.execute('SELECT status,registry_note,pitch,notes_json FROM name_candidates WHERE name=?', (name,)).fetchone()
            if not row:
                raise SystemExit(f'missing candidate {name}')
            original_reviews[name] = conn.execute('''SELECT decision,reason,review_id FROM candidate_quality_reviews
                WHERE name=? AND review_id!='20261113-mountain-manual-tranche-1' ORDER BY rowid DESC LIMIT 1''',(name,)).fetchone()
            if not original_reviews[name] or original_reviews[name][0] != 'keep':
                raise SystemExit(f'candidate lacks independent keep: {name}')
            if row[0] == '待核验' and row[1].startswith('AI 生成候选；'):
                before[name] = dict(zip(('status','registry_note','pitch','notes_json'),row))
            else:
                before[name] = {'status':'待核验',
                    'registry_note':'AI 生成候选；已通过各字语义角色及自然度初筛；仍需人工核对，并自行完成工商、商标和域名检索。',
                    'pitch':row[2], 'notes_json':row[3]}
        review_id='20261113-mountain-manual-tranche-1'
        for name,decision,reason in DECISIONS:
            status={'keep':'待核验','reject':'质量复核未通过','uncertain':'质量复核待定'}[decision]
            conn.execute('UPDATE name_candidates SET status=?,registry_note=? WHERE name=?',
                (status,'人工裁决（非最终验收）：'+reason,name))
            conn.execute('''INSERT OR REPLACE INTO candidate_quality_reviews
                (name,review_id,model,decision,reason) VALUES(?,?,?,?,?)''',
                (name,review_id,'agent-adjudication',decision,reason))
    from collections import Counter
    from scripts.review_character_candidates import phrase_references,dictionary_phrase_references
    names=[name for name,_,_ in DECISIONS]
    evidence={
      'created_at':datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
      'status':'agent_adjudicated_not_final_acceptance','final_acceptance':False,
      'review_id':'20261113-mountain-manual-tranche-1',
      'before':before,
      'reviews':[{'name':n,'decision':d,'reason':r} for n,d,r in DECISIONS],
      'counts':dict(Counter(d for _,d,_ in DECISIONS)),
      'original_independent_reviews':{n:{'decision':v[0],'reason':v[1],'review_id':v[2]} for n,v in original_reviews.items()},
      'dictionary_phrase_entries':dictionary_phrase_references(names),
      'literature_phrase_examples':phrase_references(names),
      'method':'逐项按名称本身的语义关系、自然度、可区分度及批内近似度裁决；参考文献仅核义，不要求出处。暂留仍需累计质量复核，不能据此声称全字库目标完成。',
    }
    path=ROOT/'data/quality-reviews/2026-11-13-mountain-manual-tranche-1.json'
    path.write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'names':len(DECISIONS),'counts':evidence['counts'],'output':str(path)},ensure_ascii=False))

if __name__=='__main__': main()
