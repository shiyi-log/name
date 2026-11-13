#!/usr/bin/env python3
"""Adjudicate a distinct provisional tranche of 松 names."""
import datetime,json,sqlite3,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.review_character_candidates import phrase_references,dictionary_phrase_references
from scripts.generate_all_character_candidates import load_character_names

DECISIONS=[
 ('密松','keep','“密松”可直接理解为繁密的松林或枝叶稠密的松树，修饰关系自然简洁。宋诗“阴密松成盖”及《西游记》松篁语境都可作词义背景，但不要求文献固定词条。'),
 ('五鬣松','keep','“鬣”有松针、松枝之义，“五鬣松”指五针松一类，宋诗题名“和林次中五鬣松”提供可核语境。它是明确植物称名，虽具体，仍有自然名称辨识度。'),
 ('松墅','uncertain','可解为松林旁的别墅或松间别业，但整体容易落入居住地产描述；在行业未知条件下品牌指向与通用场所称谓边界不清，暂不计数。'),
 ('松柏堂','uncertain','松柏意象与“堂”号可组成自然商号结构，但完整词面更像传统机构或堂号；当前缺少独立辨识度证据，暂列待定。'),
 ('霰松','keep','可自然理解为霰落松枝或霰中松树，天气与植物组成具体冬日物象，二字倒装可作为凝练景名；不是完整句子。'),
 ('松悦','uncertain','“悦”可表达愉悦，但与松树之间的语义关系未在名称本身落实，容易成为常见吉祥字拼合，暂不计数。'),
 ('松浒','keep','“浒”指水边，松浒可理解为水岸松树或松生水滨，地貌与植物关系清楚；字义直接，不需借用其他字义。'),
 ('松影横迳','uncertain','松影横过小径的画面可理解，但“迳”承担路径义时组合完整、偏写景；与大量已有松影场景候选接近，暂不计数。'),
 ('松影浮泉','reject','与暂留“松影浮霭”共享“松影浮＋自然名词”的同一核心结构，仅替换末字并扩展倒影介质，属于近似模板变体。'),
 ('松条雨','uncertain','可设想为雨落松枝，但“条”在名称中指枝条的用法不够稳定，组合关系需补足语境，暂不计数。'),
 ('雪松堂','uncertain','“雪松”可指树种，“堂”可作场所后缀；整体接近植物商号或场所名称，且具体所指可能由雪松树种主导，品牌辨识度存疑。'),
 ('冰泉松影','uncertain','冰泉与松影各自成景，但二者间关系不明确；与现有松影类候选高度密集，暂不计数。'),
]
def main():
 db=ROOT/'data/names.db';before={};originals={};
 counted={tuple(sorted(name)) for name,_,_ in load_character_names()}
 with sqlite3.connect(db,timeout=30) as c:
  for name,decision,reason in DECISIONS:
   row=c.execute('select status,registry_note,pitch,notes_json from name_candidates where name=?',(name,)).fetchone()
   if not row:raise SystemExit(f'missing {name}')
   old=c.execute('select decision,reason,review_id from candidate_quality_reviews where name=? order by rowid desc limit 1',(name,)).fetchone()
   if not old or old[0] not in ('uncertain','reject'):raise SystemExit(f'not a reconsideration candidate: {name}')
   originals[name]=old
   before[name]={'status':row[0],'registry_note':row[1],'pitch':row[2],'notes_json':row[3]}
   if decision=='keep' and tuple(sorted(name)) in counted:raise SystemExit(f'already counted multiset: {name}')
  for name,decision,reason in DECISIONS:
   status={'keep':'待核验','uncertain':'质量复核待定','reject':'质量复核未通过'}[decision]
   c.execute('update name_candidates set status=?,registry_note=? where name=?',(status,'人工复核复裁（非最终验收）：'+reason,name))
   c.execute('insert or replace into candidate_quality_reviews(name,review_id,model,decision,reason) values(?,?,?,?,?)',(name,'20261113-pine-manual-tranche-1','agent-adjudication',decision,reason))
 from collections import Counter
 names=[x[0] for x in DECISIONS]
 evidence={'created_at':datetime.datetime.now().astimezone().isoformat(timespec='seconds'),'status':'agent_adjudicated_not_final_acceptance','final_acceptance':False,'review_id':'20261113-pine-manual-tranche-1','before':before,'reviews':[{'name':n,'decision':d,'reason':r} for n,d,r in DECISIONS],'counts':dict(Counter(d for _,d,_ in DECISIONS)),'original_independent_reviews':{n:{'decision':v[0],'reason':v[1],'review_id':v[2]} for n,v in originals.items()},'dictionary_phrase_entries':dictionary_phrase_references(names),'literature_phrase_examples':phrase_references(names),'method':'复核以完整名称本身的词义关系、自然度、去重和可区分度为准；对待定名称重新判断，文献仅核义参考。保留为暂留，仍非最终验收。'}
 out=ROOT/'data/quality-reviews/2026-11-13-pine-manual-tranche-1.json';out.write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
 print(json.dumps({'names':len(names),'counts':evidence['counts'],'literature_matches':sum(bool(v) for v in evidence['literature_phrase_examples'].values()),'output':str(out)},ensure_ascii=False))
if __name__=='__main__':main()
