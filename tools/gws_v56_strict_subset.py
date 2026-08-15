#!/usr/bin/env python3
from __future__ import annotations

import argparse, asyncio, json, time
from collections import Counter
from pathlib import Path

import gws_no_website_certifier_v53 as prod


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--input',required=True); ap.add_argument('--outdir',required=True)
    ap.add_argument('--max',type=int,default=0)
    ap.add_argument('--shard-index',type=int,default=0); ap.add_argument('--shard-count',type=int,default=1)
    ap.add_argument('--http-concurrency',type=int,default=24); ap.add_argument('--search-concurrency',type=int,default=3)
    a=ap.parse_args()
    all_rows=[json.loads(x) for x in Path(a.input).read_text(encoding='utf-8-sig').splitlines() if x.strip()]
    if a.shard_count<1 or a.shard_index<0 or a.shard_index>=a.shard_count: raise SystemExit('INVALID_SHARD')
    rows=[x for i,x in enumerate(all_rows) if i%a.shard_count==a.shard_index]
    if a.max>0: rows=rows[:a.max]
    cands=[]; places={}
    for x in rows:
        c=dict(x.get('candidate') or {}); pe=dict(x.get('place') or {})
        c['_strict_high_candidate']=True; c['_resolved_place_evidence']=pe; cands.append(c); places[int(c['r'])]=pe
    z=time.time(); w1=asyncio.run(prod.v5.run_web(cands,a.http_concurrency,a.search_concurrency,1)); survivors=[]; out=[]
    for c in cands:
        r=int(c['r']); p=w1.get(r,{})
        if prod.obvious_non_independent_entity(c):
            out.append({'r':r,'candidate':c,'place':places[r],'status':'REJECT','reason':'OUT_OF_SCOPE_NON_INDEPENDENT_PUBLIC_ENTITY'})
        elif p.get('owned'):
            out.append({'r':r,'candidate':c,'place':places[r],'web_pass1':p,'status':'REJECT','reason':'OWNED_SITE_FREE_PASS1','owned_site':p.get('owned')})
        elif not prod.v5.coverage(p).get('ok'):
            out.append({'r':r,'candidate':c,'place':places[r],'web_pass1':p,'status':'ERROR_RETRYABLE','reason':'FREE_SEARCH_COVERAGE_INSUFFICIENT_PASS1'})
        else: survivors.append(c)
    w2=asyncio.run(prod.v5.run_web(survivors,a.http_concurrency,a.search_concurrency,2)) if survivors else {}
    for c in survivors:
        r=int(c['r']); p1=w1[r]; p2=w2.get(r,{}); cert=prod.v5.certificate(c,places[r],p1,p2)
        if p2.get('owned'): st,rs='REJECT','OWNED_SITE_FREE_PASS2'
        elif not prod.v5.coverage(p2).get('ok'): st,rs='ERROR_RETRYABLE','FREE_SEARCH_COVERAGE_INSUFFICIENT_PASS2'
        elif cert.get('unresolved_plausible_domains'): st,rs='UNCERTAIN','PLAUSIBLE_DOMAIN_UNRESOLVED'
        elif not cert.get('gates',{}).get('current_identity_strong'): st,rs='MEDIUM','IDENTITY_RESOLVED_NOT_STRONG_ENOUGH_FOR_HIGH'
        elif cert.get('verified'): st,rs='HIGH','VERIFIED_NO_WEBSITE'
        else: st,rs='MEDIUM','SURVIVED_BUT_CERTIFICATE_GATES_INCOMPLETE'
        rec={'r':r,'candidate':c,'place':places[r],'web_pass1':p1,'web_pass2':p2,'certificate':cert,'status':st,'reason':rs}
        if p2.get('owned'): rec['owned_site']=p2.get('owned')
        out.append(rec)
    out.sort(key=lambda x:int(x['r'])); counts=Counter(x['status'] for x in out); reasons=Counter(x['reason'] for x in out)
    d=Path(a.outdir); d.mkdir(parents=True,exist_ok=True)
    (d/'results.jsonl').write_text(''.join(json.dumps(x,ensure_ascii=False,separators=(',',':'),default=str)+'\n' for x in out),encoding='utf-8')
    expected=len(rows); unique=len({int(x['r']) for x in out})
    summary={'schema':'gws-v57-free-strict-shard-v1','input_rows':len(all_rows),'attempted':len(out),'shard_index':a.shard_index,'shard_count':a.shard_count,'statuses':dict(counts),'reasons':dict(reasons),'verified_no_website':counts.get('HIGH',0),'owned_sites_found':counts.get('REJECT',0),'elapsed_seconds':round(time.time()-z,2),'integrity':{'expected':expected,'returned':len(out),'unique_r':unique,'ok':expected==len(out)==unique},'zero_paid_api':True}
    (d/'summary.json').write_text(json.dumps(summary,indent=2)+'\n',encoding='utf-8')
    print('GWS_V57_FREE_SHARD='+json.dumps(summary,separators=(',',':')),flush=True)
    if not summary['integrity']['ok']: raise SystemExit('SHARD_INTEGRITY_FAILED')

if __name__=='__main__': main()
