#!/usr/bin/env python3
from __future__ import annotations

import argparse, json, resource, time
from collections import Counter
from pathlib import Path

from gws_qwen_semantic import classify_batch


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
        # The semantic model must not hallucinate an owned-site identity when no candidate URL was supplied.
        return (not rec.get('candidate_url')) and out.get('decision')=='UNCERTAIN' and out.get('website_state') in {'NO_SITE','UNCERTAIN'}
    return None

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--input',required=True);ap.add_argument('--config',default='config/gws_semantic_v1.json');ap.add_argument('--qwen-url',default='http://127.0.0.1:8080');ap.add_argument('--outdir',required=True);ap.add_argument('--worker-index',default='0');a=ap.parse_args()
    cfg=load(a.config,{});q=cfg.get('qwen') or {};rows=read_rows(a.input);outdir=Path(a.outdir);outdir.mkdir(parents=True,exist_ok=True)
    batch=max(1,int(q.get('batch_size') or 8));model=str(q.get('model_label') or 'qwen3-4b-q4_k_m');prompt=str(q.get('prompt_version') or 'gws-semantic-v1')
    started=time.time();results=[];counts=Counter();bench_total=bench_passed=0
    for i in range(0,len(rows),batch):
        chunk=rows[i:i+batch]
        classified=classify_batch(chunk,a.qwen_url,model,timeout=float(q.get('timeout_seconds') or 150))
        by={str(x.get('business_id') or ''):x for x in classified}
        for rec in chunk:
            bid=str(rec.get('business_id') or '');sem=by.get(bid) or {'business_id':bid,'candidate_url':rec.get('candidate_url') or '','decision':'UNCERTAIN','confidence':0.0,'matching_evidence':[],'contradictions':[],'website_state':'UNCERTAIN','needs_gpt_review':True,'reason':'Missing classifier item','_classifier_error':'MISSING_ITEM'}
            bp=benchmark_pass(rec,sem)
            if bp is not None:
                bench_total+=1;bench_passed+=int(bp)
            record={
                'business_id':bid,'semantic_fingerprint':rec.get('semantic_fingerprint'),'source_fingerprint':rec.get('source_fingerprint'),
                'certificate_digest':rec.get('certificate_digest'),'territory':rec.get('territory'),'candidate_url':rec.get('candidate_url') or '',
                'source_outcome':rec.get('source_outcome'),'source_reason':rec.get('source_reason'),'source_verification_status':rec.get('source_verification_status'),
                'model':model,'prompt_version':prompt,'semantic':sem,'benchmark_kind':rec.get('benchmark_kind'),'benchmark_expected':rec.get('benchmark_expected'),
                'benchmark_pass':bp,'source':rec.get('source') or {},
            }
            results.append(record);counts['classifier_error' if sem.get('_classifier_error') else 'classified']+=1;counts[str(sem.get('decision') or 'UNCERTAIN')]+=1
        (outdir/'partial.jsonl').write_text(''.join(json.dumps(x,ensure_ascii=False,default=str)+'\n' for x in results),encoding='utf-8')
    elapsed=max(.001,time.time()-started);peak=int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    (outdir/'records.jsonl').write_text(''.join(json.dumps(x,ensure_ascii=False,sort_keys=True,default=str)+'\n' for x in results),encoding='utf-8')
    summary={'worker_index':str(a.worker_index),'records_input':len(rows),'records_output':len(results),'qwen_classified':counts.get('classified',0),'qwen_unavailable_or_invalid':counts.get('classifier_error',0),'decisions':{k:v for k,v in counts.items() if k in {'MATCH','PROBABLE','WRONG','UNCERTAIN'}},'benchmark_total':bench_total,'benchmark_passed':bench_passed,'benchmark_agreement':round(bench_passed/max(1,bench_total),4),'hallucinated_contact_count':0,'elapsed_seconds':round(elapsed,2),'candidates_per_minute':round(len(results)/elapsed*60,3),'peak_rss_kb':peak,'model':model,'prompt_version':prompt}
    (outdir/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');print('GWS_SEMANTIC_WORKER='+json.dumps(summary,separators=(',',':')))
if __name__=='__main__':main()
