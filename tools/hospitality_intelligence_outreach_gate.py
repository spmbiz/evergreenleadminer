#!/usr/bin/env python3
"""Strict commercial outreach gate for Hospitality Intelligence V2.

Semantic/Qwen positives can add evidence, but they can never override the
commercial-fit gate. Only deterministic A/B commercial fit with a verified
HIGH/MEDIUM lodging identity can become outreach-ready.
"""
from __future__ import annotations
import argparse,gzip,json
from pathlib import Path
from typing import Any,Iterable
from hospitality_quality_v2 import assess_record,sanitize_social

CONTACT_FIELDS=("public_email","public_phone","instagram","facebook","whatsapp","contact_page")
GOOD_FIT={"STRONG_FIT","FIT"}
V1_LIVE={"HIGH","MEDIUM"}

def truthy(v): return bool(str(v or '').strip())
def as_int(v,d=0):
 try:return int(float(v or 0))
 except:return d
def as_float(v,d=0.0):
 try:return float(v or 0)
 except:return d

def load_config(path):
 return (json.loads(Path(path).read_text(encoding='utf-8')).get('outreach_gate') or {})
def iter_jsonl(path:Path)->Iterable[dict[str,Any]]:
 op=gzip.open if path.suffix=='.gz' else open
 with op(path,'rt',encoding='utf-8') as f:
  for line in f:
   if line.strip(): yield json.loads(line)
def load_plan_records(root:Path):
 out={}
 for p in sorted(root.rglob('shard-*.jsonl')):
  for r in iter_jsonl(p):
   aid=str(r.get('account_id') or '').strip()
   if aid:out[aid]=r
 return out
def load_v2_accounts(root:Path):
 rows=[]
 for p in sorted(root.rglob('accounts.jsonl.gz')):rows.extend(iter_jsonl(p))
 return rows

def contact_routes(v2):
 out=[]
 if truthy(v2.get('public_email')):out.append('public_email')
 if truthy(v2.get('public_phone')):out.append('public_phone')
 ig=sanitize_social(v2.get('instagram') or '','instagram')
 fb=sanitize_social(v2.get('facebook') or '','facebook')
 if ig:out.append('instagram')
 if fb:out.append('facebook')
 if truthy(v2.get('whatsapp')):out.append('whatsapp')
 if truthy(v2.get('contact_page')):out.append('contact_page')
 return out

def commercial_quality(v2,v1):
 raw=dict(v1.get('raw') or {})
 merged=dict(raw)
 for src in (v1,v2):
  for k,v in src.items():
   if v not in (None,'',[],{}): merged[k]=v
 tier=str(v1.get('commercial_fit_tier') or raw.get('commercial_fit_tier') or '').upper()
 version=str(v1.get('quality_version') or raw.get('quality_version') or '')
 sales=str(v1.get('sales_ready') or raw.get('sales_ready') or '').upper()
 if version and tier in {'A','B','C','X'}:
  return {
   'quality_version':version,'commercial_fit_tier':tier,
   'premium_score_v2':as_int(v1.get('premium_score_v2') or raw.get('premium_score_v2')),
   'operator_score_v2':as_int(v1.get('operator_score_v2') or raw.get('operator_score_v2')),
   'commercial_score':as_int(v1.get('commercial_score') or raw.get('commercial_score')),
   'sales_ready': sales in {'YES','TRUE','1'} if sales else tier in {'A','B'},
   'quality_decision':str(v1.get('quality_decision') or raw.get('quality_decision') or ('ACCEPT' if tier in {'A','B'} else 'REVIEW')),
   'quality_reason':str(v1.get('quality_reason') or raw.get('quality_reason') or 'stored_v2_quality'),
  }
 text=' '.join([
  str(v2.get('classification_reason') or ''),
  ' '.join(map(str,v2.get('matching_evidence') or [])),
  ' '.join(map(str,v2.get('sample_property_urls') or [])),
 ])
 return assess_record(merged,text)

def evaluate(v2:dict,v1:dict,cfg:dict)->dict:
 fit=str(v2.get('fit_decision') or 'MAYBE').upper()
 entity=str(v2.get('entity_match') or 'UNCERTAIN').upper()
 confidence=as_float(v2.get('confidence'))
 routes=contact_routes(v2)
 q=commercial_quality(v2,v1)
 qtier=str(q.get('commercial_fit_tier') or 'C').upper()
 qpremium=as_int(q.get('premium_score_v2'))
 qoperator=as_int(q.get('operator_score_v2'))
 quality_ready=bool(q.get('sales_ready')) and qtier in {'A','B'}
 live=str(v1.get('live_status') or (v1.get('raw') or {}).get('live_status') or '').upper()
 min_conf=as_float(cfg.get('min_confidence'),0.70)
 commercial=as_int(v2.get('commercial_score'))
 contactability=as_int(v2.get('contactability_score'))
 leverage=as_int(v2.get('portfolio_leverage_score'))
 properties=as_int(v2.get('property_count_known'))
 reasons=[]
 hard = fit=='REJECT_OBVIOUS' or entity=='WRONG' or live=='REJECT' or qtier=='X' or str(q.get('quality_decision')).upper()=='REJECT'
 if hard:
  tier='REJECT'; reasons.append('deterministic_or_semantic_reject')
 elif not quality_ready:
  tier='C'; reasons.append('commercial_fit_v2_not_sales_ready')
 elif live not in V1_LIVE:
  tier='C'; reasons.append('live_identity_not_high_or_medium')
 elif truthy(v2.get('classifier_error')) or confidence<min_conf:
  tier='C'; reasons.append('semantic_evidence_needs_review')
 elif fit not in GOOD_FIT:
  tier='C'; reasons.append('semantic_fit_not_confirmed')
 elif qtier=='A' and commercial>=80 and (properties>=5 or leverage>=65 or qoperator>=65):
  tier='S'; reasons.append('strict_v2_a_plus_strong_commercial_leverage')
 elif qtier=='A' and commercial>=55:
  tier='A'; reasons.append('strict_v2_a')
 elif qtier=='B' and commercial>=62 and contactability>=as_int(cfg.get('good_b_contactability_min'),48):
  tier='B'; reasons.append('strict_v2_b_plus_contactability')
 else:
  tier='C'; reasons.append('commercial_strength_below_outreach_floor')
 ready=tier in {'S','A','B'} and bool(routes)
 if tier in {'S','A','B'} and not routes: reasons.append('qualified_but_no_public_contact_route')
 out=dict(v2)
 out.update({
  'commercial_tier':tier,'outreach_ready':ready,'outreach_rank':{'S':4,'A':3,'B':2,'C':1,'REJECT':0}[tier],
  'outreach_reasons':reasons,'public_contact_routes':routes,
  'commercial_quality_version':q.get('quality_version'),'commercial_fit_v2':qtier,
  'commercial_quality_reason':q.get('quality_reason'),'commercial_quality_sales_ready':quality_ready,
  'v1_live_status':live,'v1_permissive_pass':False,
  'v1_operator_score':as_int(v1.get('operator_score')),'v1_premium_score':as_int(v1.get('premium_score')),
  'premium_signal':qpremium>=50,'premium_property_path':qtier=='A',
  'portfolio_signal':properties>=3 or leverage>=55 or qoperator>=60,
 })
 out['instagram']=sanitize_social(v2.get('instagram') or '','instagram')
 out['facebook']=sanitize_social(v2.get('facebook') or '','facebook')
 return out

def write_jsonl(path,rows,gzip_output=False):
 op=gzip.open if gzip_output else open
 mode='wt' if gzip_output else 'w'
 with op(path,mode,encoding='utf-8') as f:
  for r in rows:f.write(json.dumps(r,ensure_ascii=False)+'\n')

def main():
 ap=argparse.ArgumentParser();ap.add_argument('--results-root',required=True);ap.add_argument('--plan-root',required=True);ap.add_argument('--config',required=True);ap.add_argument('--outdir',required=True);a=ap.parse_args()
 cfg=load_config(a.config);outdir=Path(a.outdir);outdir.mkdir(parents=True,exist_ok=True)
 v1=load_plan_records(Path(a.plan_root));v2=load_v2_accounts(Path(a.results_root))
 scored=[evaluate(r,v1.get(str(r.get('account_id') or ''),{}),cfg) for r in v2]
 scored.sort(key=lambda r:(-as_int(r.get('outreach_rank')),-as_int(r.get('commercial_score')),str(r.get('domain') or '')))
 ready=[r for r in scored if r.get('outreach_ready')];follow=[r for r in scored if not r.get('outreach_ready') and r.get('commercial_tier')!='REJECT'];rej=[r for r in scored if r.get('commercial_tier')=='REJECT']
 write_jsonl(outdir/'outreach-tiered.jsonl.gz',scored,True);write_jsonl(outdir/'outreach-ready.jsonl',ready);write_jsonl(outdir/'outreach-followup.jsonl',follow);write_jsonl(outdir/'outreach-rejected.jsonl',rej)
 tiers={t:sum(r.get('commercial_tier')==t for r in scored) for t in ('S','A','B','C','REJECT')}
 summary={'accounts_scored':len(scored),'tiers':tiers,'outreach_ready':len(ready),'outreach_ready_s':sum(r.get('commercial_tier')=='S' for r in ready),'outreach_ready_a':sum(r.get('commercial_tier')=='A' for r in ready),'outreach_ready_b':sum(r.get('commercial_tier')=='B' for r in ready),'strict_commercial_fit_v2':True,'permissive_v1_allowed':False}
 (outdir/'outreach-summary.json').write_text(json.dumps(summary,indent=2)+'\n',encoding='utf-8');print(json.dumps(summary))
if __name__=='__main__':main()
