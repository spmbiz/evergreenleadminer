#!/usr/bin/env python3
"""Remove already-canonical website domains before expensive HTTP work.

The canonical snapshot is advisory/read-only. Final canonicalization still happens
at the single writer, so a stale snapshot can only cause extra work, never a bad
append. This filter never removes rows without a determinable domain.
"""
from __future__ import annotations
import argparse,csv,json,re
from pathlib import Path
from urllib.parse import urlparse

MULTI=("co.uk","org.uk","me.uk","ltd.uk","plc.uk","net.uk","com.au","net.au","org.au","com.br","com.mx","co.nz","net.nz","org.nz","co.za","com.pt","com.es","com.tr","co.jp","com.sg","com.hk","com.my")

def root_host(h):
    h=(h or '').lower().strip('.')
    if h.startswith('www.'):h=h[4:]
    if not h:return ''
    for s in MULTI:
        if h==s:return h
        if h.endswith('.'+s):
            p=h.split('.');return '.'.join(p[-3:])
    p=h.split('.');return '.'.join(p[-2:]) if len(p)>=2 else h

def domain(row):
    d=root_host(str(row.get('domain') or '').strip())
    if d:return d
    try:return root_host(urlparse(str(row.get('website') or '')).hostname or '')
    except:return ''

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--input',required=True);ap.add_argument('--domains',required=True);ap.add_argument('--stats',required=True);a=ap.parse_args()
    inp=Path(a.input); domp=Path(a.domains); statp=Path(a.stats);statp.parent.mkdir(parents=True,exist_ok=True)
    known=set()
    if domp.exists():
        import gzip
        opener=gzip.open if domp.suffix=='.gz' else open
        with opener(domp,'rt',encoding='utf-8') as f:
            for line in f:
                d=root_host(line.strip())
                if d:known.add(d)
    if not inp.exists():
        stat={'input_rows':0,'kept_rows':0,'canonical_domain_rejected':0,'known_domains':len(known),'missing_input':True};statp.write_text(json.dumps(stat,indent=2)+'\n');print(json.dumps(stat));return
    with inp.open(encoding='utf-8',newline='') as f: rows=list(csv.DictReader(f)); fields=list(rows[0].keys()) if rows else []
    kept=[];rejected=0
    for r in rows:
        d=domain(r)
        if d and d in known:rejected+=1
        else:kept.append(r)
    tmp=inp.with_suffix(inp.suffix+'.tmp')
    with tmp.open('w',encoding='utf-8',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(kept)
    tmp.replace(inp)
    stat={'input_rows':len(rows),'kept_rows':len(kept),'canonical_domain_rejected':rejected,'known_domains':len(known),'missing_input':False}
    statp.write_text(json.dumps(stat,indent=2)+'\n',encoding='utf-8');print(json.dumps(stat))
if __name__=='__main__':main()
