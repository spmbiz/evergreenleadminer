#!/usr/bin/env python3
"""Probe the latest AllThePlaces archive through HTTP range reads only.

This does not download the 2+ GB archive. It opens the remote ZIP as a seekable
file, reads its central directory, and reports member metadata so source
partitioning can be designed from real archive structure.
"""
from __future__ import annotations
import argparse,json,re,zipfile
from pathlib import Path
import requests
import fsspec

INFO='https://data.alltheplaces.xyz/runs/latest/info_embed.html'
KEYWORDS=re.compile(r'(hotel|resort|vacation|holiday|villa|chalet|lodg|motel|hostel|stay|rent|apart|cottage|bnb|bed.?and.?breakfast)',re.I)

def latest_url():
    r=requests.get(INFO,timeout=15);r.raise_for_status()
    m=re.search(r'href=["\'](https://alltheplaces-data\.openaddresses\.io/runs/[^"\']+/output\.zip)',r.text,re.I)
    if not m: raise RuntimeError('latest output.zip URL not found')
    return m.group(1)

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--out',required=True);ap.add_argument('--block-mb',type=int,default=4);a=ap.parse_args()
    url=latest_url()
    with fsspec.open(url,'rb',block_size=a.block_mb*1024*1024,cache_type='readahead').open() as remote:
        if not remote.seekable():raise RuntimeError('remote ATP file is not seekable')
        with zipfile.ZipFile(remote) as z:
            infos=[i for i in z.infolist() if not i.is_dir()]
            geo=[i for i in infos if i.filename.lower().endswith(('.geojson','.json','.geojson.gz','.ndjson','.ndjson.gz'))]
            hits=[i for i in geo if KEYWORDS.search(Path(i.filename).name)]
            payload={
              'latest_url':url,
              'members_total':len(infos),
              'data_members':len(geo),
              'compressed_bytes_total':sum(i.compress_size for i in geo),
              'uncompressed_bytes_total':sum(i.file_size for i in geo),
              'keyword_member_count':len(hits),
              'keyword_members':[{'name':i.filename,'compressed':i.compress_size,'uncompressed':i.file_size} for i in sorted(hits,key=lambda x:x.compress_size,reverse=True)[:100]],
              'largest_members':[{'name':i.filename,'compressed':i.compress_size,'uncompressed':i.file_size} for i in sorted(geo,key=lambda x:x.compress_size,reverse=True)[:30]],
            }
    p=Path(a.out);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(payload,indent=2)+'\n',encoding='utf-8');print(json.dumps(payload,indent=2))
if __name__=='__main__':main()
