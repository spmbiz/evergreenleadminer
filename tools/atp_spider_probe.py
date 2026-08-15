#!/usr/bin/env python3
"""Inspect a few features from official AllThePlaces per-spider outputs."""
from __future__ import annotations
import argparse,json
from pathlib import Path
import requests,ijson

ROOT='https://data.alltheplaces.xyz/runs/latest/output/{spider}.geojson'
KEEP=('name','brand','operator','website','email','phone','addr:city','addr:state','addr:country','addr:full','street_address','city','state','country','@spider','@source_uri','ref','id','category','tourism')

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--spiders',required=True);ap.add_argument('--out',required=True);ap.add_argument('--sample',type=int,default=5);a=ap.parse_args()
    out={'spiders':{}}
    s=requests.Session();s.headers['User-Agent']='AIProdLeadHarvester/1.0 (+public-data-research)'
    for spider in [x.strip() for x in a.spiders.split(',') if x.strip()]:
        url=ROOT.format(spider=spider)
        item={'request_url':url,'samples':[]}
        try:
            with s.get(url,stream=True,timeout=30,allow_redirects=True) as r:
                item['status']=r.status_code;item['final_url']=r.url;item['content_type']=r.headers.get('Content-Type','');r.raise_for_status();r.raw.decode_content=True
                for feature in ijson.items(r.raw,'features.item'):
                    props=feature.get('properties') if isinstance(feature,dict) else {}
                    item['samples'].append({k:props.get(k) for k in KEEP if isinstance(props,dict) and k in props})
                    if len(item['samples'])>=a.sample:break
        except Exception as e:item['error']=f'{type(e).__name__}: {e}'
        out['spiders'][spider]=item
    p=Path(a.out);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(out,indent=2,default=str)+'\n',encoding='utf-8');print(json.dumps(out,indent=2,default=str))
if __name__=='__main__':main()
