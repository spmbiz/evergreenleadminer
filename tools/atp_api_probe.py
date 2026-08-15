#!/usr/bin/env python3
"""Probe the official AllThePlaces run/spider API without downloading bulk data."""
from __future__ import annotations
import argparse,json
from pathlib import Path
import requests

ROOT='https://data.alltheplaces.xyz'

def describe(obj,limit=25):
    if isinstance(obj,dict):
        return {'type':'dict','count':len(obj),'keys':list(obj.keys())[:limit]}
    if isinstance(obj,list):
        return {'type':'list','count':len(obj),'sample':obj[:min(limit,len(obj))]}
    return {'type':type(obj).__name__,'value':str(obj)[:500]}

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--out',required=True);a=ap.parse_args()
    s=requests.Session();s.headers['User-Agent']='AIProdLeadHarvester/1.0 (+public-data-research)'
    r=s.get(ROOT+'/runs/latest.json',timeout=20);r.raise_for_status();meta=r.json()
    payload={'latest':meta,'latest_shape':describe(meta)}
    for key in ('stats_url','insights_url'):
        url=meta.get(key)
        if not url:continue
        try:
            rr=s.get(url,timeout=30);rr.raise_for_status();obj=rr.json()
            payload[key]={'url':url,'shape':describe(obj,50)}
            if isinstance(obj,dict):
                # Keep only small structural hints, never dump a huge stats file.
                small={}
                for k,v in list(obj.items())[:20]:small[k]=describe(v,10)
                payload[key]['children']=small
        except Exception as e:
            payload[key]={'url':url,'error':f'{type(e).__name__}: {e}'}
    p=Path(a.out);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(payload,indent=2)+'\n',encoding='utf-8');print(json.dumps(payload,indent=2))
if __name__=='__main__':main()
