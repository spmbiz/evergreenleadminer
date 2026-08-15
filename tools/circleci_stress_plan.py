#!/usr/bin/env python3
"""Deterministic stress-only planner: select real catalog shards without coverage/offset.

This is intentionally not production scheduling logic. It exists to prove actual
CircleCI compute/concurrency and compare verifier engines on known disjoint work.
"""
from __future__ import annotations
import argparse, datetime as dt, json
from pathlib import Path
import fleet_runtime as fr


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--capacity',type=int,required=True)
    ap.add_argument('--local-workers',type=int,default=64)
    ap.add_argument('--out',required=True)
    a=ap.parse_args()
    rows=sorted(fr.catalog(), key=lambda s:(s.get('country',''),s.get('region',''),s.get('key','')))
    if len(rows)<a.capacity:
        raise SystemExit(f'catalog too small: {len(rows)} < {a.capacity}')
    selected=[]
    for i,s in enumerate(rows[:a.capacity]):
        x=dict(s);x['slot']=i;x['local_workers']=a.local_workers;selected.append(x)
    cycle=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-circleci-stress'
    payload={'enabled':True,'cycle_id':cycle,'provider':'circleci','capacity':a.capacity,'catalog_size':len(rows),'include':selected}
    p=Path(a.out);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(payload,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'catalog_size':len(rows),'selected':len(selected),'cycle_id':cycle}))

if __name__=='__main__': main()
