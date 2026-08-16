#!/usr/bin/env python3
from __future__ import annotations

import argparse, datetime as dt, json
from collections import Counter
from pathlib import Path


def load(path,default):
    try:return json.loads(Path(path).read_text(encoding='utf-8'))
    except Exception:return default

def dump(path,obj):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(obj,ensure_ascii=False,indent=2,sort_keys=True)+'\n',encoding='utf-8')
def append(path,rows):
    if not rows:return
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
    with p.open('a',encoding='utf-8') as f:
        for r in rows:f.write(json.dumps(r,ensure_ascii=False,sort_keys=True,default=str)+'\n')
def now():return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()

def worker_records(root):
    for p in sorted(Path(root).rglob('records.jsonl')):
        for line in p.read_text(encoding='utf-8').splitlines():
            if line.strip():yield json.loads(line)
def worker_summaries(root):
    for p in sorted(Path(root).rglob('summary.json')):
        try:yield json.loads(p.read_text(encoding='utf-8'))
        except Exception:continue

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--root',required=True);ap.add_argument('--plan',required=True);ap.add_argument('--config',default='config/gws_semantic_v1.json');a=ap.parse_args()
    cfg=load(a.config,{});plan=load(a.plan,{});stage=str(plan.get('stage') or 'smoke');rows=list(worker_records(a.root));summ=list(worker_summaries(a.root));ts=now();date=ts[:10]
    index=load('state/gws_semantic_index.json',{'schema_version':1,'records':{}});records=index.setdefault('records',{})
    review=[];decisions=Counter();states=Counter();classified=errors=0;bench_total=bench_passed=0
    for r in rows:
        bid=str(r.get('business_id') or '');sem=r.get('semantic') or {};err=str(sem.get('_classifier_error') or '')
        if not bid:continue
        classified+=int(not err);errors+=int(bool(err));decisions[str(sem.get('decision') or 'UNCERTAIN')]+=1;states[str(sem.get('website_state') or 'UNCERTAIN')]+=1
        if r.get('benchmark_pass') is not None:
            bench_total+=1;bench_passed+=int(bool(r.get('benchmark_pass')))
        records[bid]={
            'semantic_fingerprint':r.get('semantic_fingerprint'),'source_fingerprint':r.get('source_fingerprint'),'certificate_digest':r.get('certificate_digest'),
            'model':r.get('model'),'prompt_version':r.get('prompt_version'),'decision':sem.get('decision'),'confidence':sem.get('confidence'),
            'website_state':sem.get('website_state'),'needs_gpt_review':bool(sem.get('needs_gpt_review')),'last_classified':ts,'classifier_error':err,
            'source_outcome':r.get('source_outcome'),'source_reason':r.get('source_reason'),'candidate_url':r.get('candidate_url') or '',
        }
        is_live=r.get('source_outcome') in set(cfg.get('selection',{}).get('live_outcomes') or []) or r.get('source_verification_status') in set(cfg.get('selection',{}).get('live_statuses') or [])
        if is_live and (err or sem.get('needs_gpt_review') or sem.get('decision') in {'WRONG','UNCERTAIN'} or float(sem.get('confidence') or 0)<0.75 or sem.get('contradictions')):
            review.append(r)
    append(Path('data/gws/semantic')/f'{date}.jsonl',rows)
    if review:
        stamp=dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H%M%SZ');path=Path('gpt/gws_semantic_review')/f'{stamp}.jsonl';append(path,review)
    dump('state/gws_semantic_index.json',index)

    total=len(rows);valid_rate=classified/max(1,total);agreement=bench_passed/max(1,bench_total);halluc=sum(int(x.get('hallucinated_contact_count') or 0) for x in summ)
    rcfg=cfg.get('rollout') or {};prior=load('state/gws_semantic_rollout.json',{})
    next_stage=stage;reason='hold'
    if stage=='smoke':
        if total>=min(16,int(rcfg.get('smoke_records') or 32)) and valid_rate>=0.90 and halluc==0:
            next_stage='benchmark';reason='smoke_passed'
        else: next_stage='smoke';reason='smoke_not_ready'
    elif stage=='benchmark':
        enough=bench_total>=int(rcfg.get('min_trusted_benchmark') or 200)
        good_schema=valid_rate>=float(rcfg.get('min_schema_valid_rate') or .99)
        good_agreement=agreement>=float(rcfg.get('min_trusted_agreement') or .92)
        if enough and good_schema and good_agreement and halluc<=int(rcfg.get('max_hallucinated_contact_count') or 0):
            next_stage='production';reason='benchmark_passed'
        else: next_stage='benchmark_waiting';reason='benchmark_gate_not_met'
    elif stage=='production': next_stage='production';reason='production_shadow'
    rollout={
        'schema_version':1,'stage':next_stage,'previous_stage':stage,'updated_at':ts,'reason':reason,
        'last_run':{'selected':int(plan.get('selected') or 0),'processed':total,'classified':classified,'errors':errors,'schema_valid_rate':round(valid_rate,4),'benchmark_total':bench_total,'benchmark_passed':bench_passed,'trusted_agreement':round(agreement,4),'hallucinated_contact_count':halluc,'decisions':dict(decisions),'website_states':dict(states)},
        'model':cfg.get('qwen',{}).get('model_label'),'prompt_version':cfg.get('qwen',{}).get('prompt_version'),
        'backlog':{'eligible_live':int(plan.get('eligible_live') or 0),'eligible_benchmark':int(plan.get('eligible_benchmark') or 0)}
    }
    dump('state/gws_semantic_rollout.json',rollout)
    metrics={'schema_version':1,'at':ts,'stage':stage,'next_stage':next_stage,'rollout_reason':reason,'processed':total,'qwen_classified':classified,'qwen_unavailable_or_invalid':errors,'schema_valid_rate':round(valid_rate,4),'benchmark_total':bench_total,'benchmark_passed':bench_passed,'trusted_agreement':round(agreement,4),'hallucinated_contact_count':halluc,'decisions':dict(decisions),'website_states':dict(states),'gpt_review_added':len(review),'canonical_high_mutations':0,'strict_high_overrides':0}
    dump('metrics/gws_semantic_latest.json',metrics);append('metrics/gws_semantic_history.jsonl',[metrics]);print('GWS_SEMANTIC_AGG='+json.dumps(metrics,separators=(',',':')))
if __name__=='__main__':main()
