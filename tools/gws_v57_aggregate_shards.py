#!/usr/bin/env python3
from __future__ import annotations

import argparse, json
from collections import Counter
from pathlib import Path


def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--root',required=True); ap.add_argument('--expected',type=int,required=True); ap.add_argument('--outdir',required=True); a=ap.parse_args()
    root=Path(a.root); files=sorted(root.rglob('results.jsonl'))
    if not files: raise SystemExit('NO_SHARD_RESULTS')
    by={}; dup=[]
    for f in files:
        for line in f.read_text(encoding='utf-8-sig').splitlines():
            if not line.strip(): continue
            x=json.loads(line); r=int(x['r'])
            if r in by: dup.append(r)
            by[r]=x
    rows=[by[k] for k in sorted(by)]; counts=Counter(x.get('status') for x in rows); reasons=Counter(x.get('reason') for x in rows)
    d=Path(a.outdir); d.mkdir(parents=True,exist_ok=True)
    def dump(name,arr): (d/name).write_text(''.join(json.dumps(x,ensure_ascii=False,separators=(',',':'),default=str)+'\n' for x in arr),encoding='utf-8')
    dump('results.jsonl',rows); dump('high_candidates.jsonl',[x for x in rows if x.get('status')=='HIGH']); dump('owned_site_rejects.jsonl',[x for x in rows if x.get('status')=='REJECT' and x.get('owned_site')])
    summary={'schema':'gws-v57-free-86-aggregate-v1','expected':a.expected,'returned':len(rows),'unique_r':len(by),'duplicates':sorted(set(dup)),'statuses':dict(counts),'reasons':dict(reasons),'verified_no_website':counts.get('HIGH',0),'owned_sites_found':counts.get('REJECT',0),'integrity':{'ok':len(rows)==a.expected and len(by)==a.expected and not dup,'shard_files':len(files)},'zero_paid_api':True,'canonical_persisted':False,'note':'Calibration aggregate only. Blind GPT red-team must complete before canonical HIGH persistence.'}
    (d/'summary.json').write_text(json.dumps(summary,indent=2)+'\n',encoding='utf-8'); print('GWS_V57_AGGREGATE='+json.dumps(summary,separators=(',',':')),flush=True)
    if not summary['integrity']['ok']: raise SystemExit('AGGREGATE_INTEGRITY_FAILED')

if __name__=='__main__': main()
