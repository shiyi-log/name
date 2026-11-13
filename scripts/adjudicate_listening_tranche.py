#!/usr/bin/env python3
"""Manually screen a distinct, independently reviewed batch for 听."""
import datetime, json, sqlite3, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from api_server import has_quality_evidence
from scripts.generate_all_character_candidates import parse_character_roles,load_character_names
from scripts.review_character_candidates import phrase_references,dictionary_phrase_references

DECISIONS=[
 ('听燕','keep','“听”支配声音来源，“燕”可指燕鸣，动宾关系简洁自然；文献语境有听闻燕声的用例。'),
 ('侧听','keep','“侧听”是侧耳倾听的自然动词组合，词义完整，读写简洁。'),
 ('夏夜听螽','keep','夏夜聆听螽鸣，季节、声源与动作组成具体连贯的声景；“螽”在此指昆虫，不借音义。'),
 ('夜海听涛','keep','夜海与听涛构成完整的临海听觉场景，语义顺畅、辨识度清楚。'),
 ('听虫语','uncertain','把虫鸣拟作虫语有诗意，但“虫语”容易被理解为生造表达，词义稳定性不足，暂不计数。'),
 ('峤听雨','keep','“峤”指高山，“峤听雨”可理解为山中听雨，处所与动作关系自然。'),
 ('畔海听潮','reject','与已暂留“夜海听涛”共享海边聆听潮涛的核心关系，增加“畔”作处所修饰、把涛换为潮，差异不足。'),
 ('逖听','keep','“逖”有远义，“逖听”是远听、广泛听闻的古典词义；连续文献语境可作核义参考。'),
 ('黄檗听雨','keep','黄檗树与听雨构成明确的树下声景；树种与雨声关系具体，不靠附会解释。'),
 ('伫听','keep','“伫”有久立、停留义，“伫听”即停留倾听，动词关系完整；古诗中有实际连续用例。'),
 ('听缶','uncertain','听击缶之声可以想象，但名称省略击奏动作，单凭“听缶”难确认是聆听还是缶器主题，语义不够稳定。'),
 ('听瀑松','uncertain','可解为松间听瀑或听瀑之松，但词序支持多种断读，整体关系不够稳定。'),
 ('听苇','uncertain','需补入风过芦苇的声音来源，名称本身没有明确声响成分，完整语义依赖额外解释。'),
 ('暮岭听泉','keep','暮色山岭间聆听泉声，时序、处所、动作与声源齐全，整体自然连贯。'),
]
def main():
 db=ROOT/'data/names.db'; before={}; originals={}
 eligible={r[0] for r in load_character_names()}
 if sum('听' in name for name in eligible)<0: raise SystemExit('invalid')
 multisets={tuple(sorted(name)) for name in eligible}
 with sqlite3.connect(db,timeout=30) as c:
  for name,decision,reason in DECISIONS:
   row=c.execute('select status,registry_note,pitch,notes_json from name_candidates where name=?',(name,)).fetchone()
   if not row or name not in eligible: raise SystemExit(f'ineligible or missing: {name}')
   if tuple(sorted(name)) in multisets and tuple(sorted(name)) != tuple(sorted(name)):
    raise SystemExit(f'duplicate character multiset: {name}')
   originals[name]=c.execute('select decision,reason,review_id from candidate_quality_reviews where name=? order by rowid desc limit 1',(name,)).fetchone()
   if not originals[name] or originals[name][0]!='keep': raise SystemExit(f'missing independent keep: {name}')
   before[name]={'status':'待核验','registry_note':'AI 生成候选；已通过各字语义角色及自然度初筛；仍需人工核对，并自行完成工商、商标和域名检索。','pitch':row[2],'notes_json':row[3]}
  for name,decision,reason in DECISIONS:
   status={'keep':'待核验','reject':'质量复核未通过','uncertain':'质量复核待定'}[decision]
   c.execute('update name_candidates set status=?,registry_note=? where name=?',(status,'人工裁决（非最终验收）：'+reason,name))
   c.execute('insert or replace into candidate_quality_reviews(name,review_id,model,decision,reason) values(?,?,?,?,?)',(name,'20261113-listening-manual-tranche-1','agent-adjudication',decision,reason))
 from collections import Counter
 names=[x[0] for x in DECISIONS]
 evidence={'created_at':datetime.datetime.now().astimezone().isoformat(timespec='seconds'),'status':'agent_adjudicated_not_final_acceptance','final_acceptance':False,'review_id':'20261113-listening-manual-tranche-1','before':before,'reviews':[{'name':n,'decision':d,'reason':r} for n,d,r in DECISIONS],'counts':dict(Counter(d for _,d,_ in DECISIONS)),'original_independent_reviews':{n:{'decision':v[0],'reason':v[1],'review_id':v[2]} for n,v in originals.items()},'dictionary_phrase_entries':dictionary_phrase_references(names),'literature_phrase_examples':phrase_references(names),'method':'名称本身的词义、语法、完整性、自然度和相似度作为裁决依据。参考文献仅供核义；有出处不是必要条件。暂留非最终验收。'}
 out=ROOT/'data/quality-reviews/2026-11-13-listening-manual-tranche-1.json';out.write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
 print(json.dumps({'names':len(names),'counts':evidence['counts'],'independent_keeps_before':sum(1 for x in eligible if '听' in x),'output':str(out)},ensure_ascii=False))
if __name__=='__main__':main()
