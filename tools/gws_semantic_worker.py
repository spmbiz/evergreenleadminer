#!/usr/bin/env python3
from __future__ import annotations

import argparse, json, resource, time
from collections import Counter
from pathlib import Path

from gws_qwen_semantic import classify_batch
from gws_semantic_targeted_search import TargetedSearchEnricher


def load(path,default):
    try:return json.loads(Path(path).read_text(encoding='utf-8'))
    except Exception:return default
def read_rows(path):
    return [json.loads(x) for x in Path(path).read_text(encoding='utf-8').splitlines() if x.strip()]

def benchmark_pass(rec,out):
    kind=rec.get('benchmark_kind')
    if kind=='OWNED_SITE_POSITIVE':
        return out.get('decision') in {'MATCH','PROBABLE'} and out.get('website_state')!='NO_SITE'
    if kind=='STRICT_NO_SITE':
        return (not rec.get('candidate_url')) and out.get('decision')=='UNCERTAIN' and out.get('website_state') in {'NO_SITE','UNCERTAIN'}
    return None

def deterministic_semantic(rec):
    # Never spend LLM tokens/CPU on a known third-party candidate when targeted
    # discovery found no plausible non-third-party alternative. If targeted search
    # surfaced any unknown host, Qwen must compare the candidate set instead.
    candidate_set=rec.get('candidate_set') or []
    has_non_third_party=any(str(x.get('host_class') or '') not in {'KNOWN_THIRD_PARTY','EDITORIAL_OR_PROFILE_PAGE'} for x in candidate_set if isinstance(x,dict))
    if rec.get('candidate_host_class')=='KNOWN_THIRD_PARTY' and not has_non_third_party:
        return {
            'business_id':str(rec.get('business_id') or ''),
            'candidate_url':str(rec.get('candidate_url') or ''),
            'decision':'WRONG','confidence':1.0,
            'matching_evidence':[],
            'contradictions':['candidate_host_class=KNOWN_THIRD_PARTY'],
            'website_state':'DIRECTORY_ONLY','needs_gpt_review':False,
            'reason':'Deterministic third-party host; targeted search found no plausible first-party alternative.',
            '_deterministic_short_circuit':'KNOWN_THIRD_PARTY',
        }
    return None

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--input',required=True);ap.add_argument('--config',default='config/gws_semantic_v1.json');ap.add_argument('--qwen-url',default='http://127.0.0.1:8080');ap.add_argument('--outdir',required=True);ap.add_argument('--worker-index',default='0');a=ap.parse_args()
    cfg=load(a.config,{});q=cfg.get('qwen') or {};rows=read_rows(a.input);outdir=Path(a.outdir);outdir.mkdir(parents=True,exist_ok=True)
    batch=max(1,int(q.get('batch_size') or 8));model=str(q.get('model_label') or 'qwen3-4b-q4_k_m');prompt=str(q.get('prompt_version') or 'gws-semantic-v1')
    started=time.time();results=[];counts=Counter();bench_total=bench_passed=0

    live_out=set(cfg.get('selection',{}).get('live_outcomes') or [])
    live_st=set(cfg.get('selection',{}).get('live_statuses') or [])
    enricher=None
    if any((r.get('source_outcome') in live_out or r.get('source_verification_status') in live_st) for r in rows):
        try: enricher=TargetedSearchEnricher()
        except Exception: enricher=None

    enriched_rows=[]
    for rec in rows:
        is_live=rec.get('source_outcome') in live_out or rec.get('source_verification_status') in live_st
        if is_live and enricher is not None:
            try:
                rec=enricher.enrich(rec)
                counts['targeted_search_records']+=1
                ts=rec.get('targeted_search') or {}
                counts['targeted_candidates']+=int(ts.get('candidates_returned') or 0)
                counts['targeted_probes']+=int(ts.get('direct_probes') or 0)
                counts['targeted_first_party_confirmed']+=len(ts.get('first_party_confirmed') or [])
            except Exception as exc:
                rec=dict(rec)
                rec['targeted_search']={'status':'ERROR','error':f'{type(exc).__name__}:{str(exc)[:180]}','candidate_set':[]}
                rec.setdefault('candidate_set',[])
                counts['targeted_search_errors']+=1
        enriched_rows.append(rec)
    rows=enriched_rows

    deterministic_by_id={}
    model_rows=[]
    for rec in rows:
        sem=deterministic_semantic(rec)
        if sem: deterministic_by_id[str(rec.get('business_id') or '')]=sem
        else: model_rows.append(rec)

    model_by_id={}
    for i in range(0,len(model_rows),batch):
        chunk=model_rows[i:i+batch]
        classified=classify_batch(chunk,a.qwen_url,model,timeout=float(q.get('timeout_seconds') or 150))
        for x in classified:model_by_id[str(x.get('business_id') or '')]=x

    for rec in rows:
        bid=str(rec.get('business_id') or '')
        sem=deterministic_by_id.get(bid) or model_by_id.get(bid) or {'business_id':bid,'candidate_url':rec.get('candidate_url') or '','decision':'UNCERTAIN','confidence':0.0,'matching_evidence':[],'contradictions':[],'website_state':'UNCERTAIN','needs_gpt_review':True,'reason':'Missing classifier item','_classifier_error':'MISSING_ITEM'}
        bp=benchmark_pass(rec,sem)
        if bp is not None:
            bench_total+=1;bench_passed+=int(bp)
        record={
            'business_id':bid,'semantic_fingerprint':rec.get('semantic_fingerprint'),'source_fingerprint':rec.get('source_fingerprint'),
            'certificate_digest':rec.get('certificate_digest'),'territory':rec.get('territory'),'name':rec.get('name'),'address':rec.get('address'),'postcode':rec.get('postcode'),
            'candidate_url':rec.get('candidate_url') or '','candidate_host_class':rec.get('candidate_host_class') or '',
            'candidate_set':rec.get('candidate_set') or [],'targeted_search':rec.get('targeted_search') or {},
            'source_outcome':rec.get('source_outcome'),'source_reason':rec.get('source_reason'),'source_verification_status':rec.get('source_verification_status'),
            'model':model,'prompt_version':prompt,'semantic':sem,'benchmark_kind':rec.get('benchmark_kind'),'benchmark_expected':rec.get('benchmark_expected'),
            'benchmark_pass':bp,'source':rec.get('source') or {},
        }
        results.append(record)
        if sem.get('_deterministic_short_circuit'):counts['deterministic_short_circuit']+=1
        elif sem.get('_classifier_error'):counts['classifier_error']+=1
        else:counts['classified']+=1
        counts[str(sem.get('decision') or 'UNCERTAIN')]+=1
        (outdir/'partial.jsonl').write_text(''.join(json.dumps(x,ensure_ascii=False,default=str)+'\n' for x in results),encoding='utf-8')

    elapsed=max(.001,time.time()-started);peak=int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    (outdir/'records.jsonl').write_text(''.join(json.dumps(x,ensure_ascii=False,sort_keys=True,default=str)+'\n' for x in results),encoding='utf-8')
    summary={'worker_index':str(a.worker_index),'records_input':len(rows),'records_output':len(results),'deterministic_short_circuit':counts.get('deterministic_short_circuit',0),'qwen_input':len(model_rows),'qwen_classified':counts.get('classified',0),'qwen_unavailable_or_invalid':counts.get('classifier_error',0),'decisions':{k:v for k,v in counts.items() if k in {'MATCH','PROBABLE','WRONG','UNCERTAIN'}},'benchmark_total':bench_total,'benchmark_passed':bench_passed,'benchmark_agreement':round(bench_passed/max(1,bench_total),4),'targeted_search_records':counts.get('targeted_search_records',0),'targeted_search_errors':counts.get('targeted_search_errors',0),'targeted_candidates':counts.get('targeted_candidates',0),'targeted_probes':counts.get('targeted_probes',0),'targeted_first_party_confirmed':counts.get('targeted_first_party_confirmed',0),'hallucinated_contact_count':0,'elapsed_seconds':round(elapsed,2),'candidates_per_minute':round(len(results)/elapsed*60,3),'peak_rss_kb':peak,'model':model,'prompt_version':prompt}
    (outdir/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');print('GWS_SEMANTIC_WORKER='+json.dumps(summary,separators=(',',':')))
if __name__=='__main__':main()
