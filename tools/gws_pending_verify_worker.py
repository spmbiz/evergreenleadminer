#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import time
from collections import Counter
from pathlib import Path

import duckdb
import gws_fleet_worker as base

DEFAULT_RELEASE="2026-06-17.0"
BBOX=(4.20,50.75,4.55,50.95)


def place_address(v):
    x=(v[0] if isinstance(v,(list,tuple)) and v else v)
    if isinstance(x,dict):
        vals=[base.txt(x.get(k)) for k in ("freeform","street","house_number","postcode","locality","region","country") if x.get(k)]
        return " ".join(vals) if vals else base.txt(json.dumps(x,ensure_ascii=False,default=str))
    return base.txt(x)


def load_places(ids,release,threads):
    if not ids: return {}
    esc=",".join("'"+str(x).replace("'","''")+"'" for x in sorted(ids))
    w,s,e,n=BBOX
    path=f"s3://overturemaps-us-west-2/release/{release}/theme=places/type=place/*"
    con=duckdb.connect(); con.execute(f"PRAGMA threads={max(1,threads)}"); con.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2';")
    q=f"""SELECT id,names.primary AS name,addresses,phones,operating_status,confidence FROM read_parquet('{path}',hive_partitioning=1)
    WHERE bbox.xmax>={w} AND bbox.xmin<={e} AND bbox.ymax>={s} AND bbox.ymin<={n} AND id IN ({esc})"""
    cur=con.execute(q); cols=[d[0] for d in cur.description]
    return {str(r[0]):dict(zip(cols,r)) for r in cur.fetchall()}


def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--input",required=True); ap.add_argument("--worker-index",type=int,required=True); ap.add_argument("--worker-count",type=int,required=True); ap.add_argument("--outdir",required=True); ap.add_argument("--release",default=DEFAULT_RELEASE); ap.add_argument("--threads",type=int,default=8); a=ap.parse_args()
    all_rows=[json.loads(x) for x in Path(a.input).read_text(encoding="utf-8").splitlines() if x.strip()]
    part=[]
    for row in all_rows:
        key=str(row.get("record_key") or row.get("hub_objectid") or "")
        bucket=int(__import__('hashlib').sha256(key.encode()).hexdigest()[:8],16)%max(1,a.worker_count)
        if bucket==a.worker_index: part.append(row)
    ids={str(x.get("overture_id")) for x in part if x.get("overture_id")}
    z=time.time(); places=load_places(ids,a.release,a.threads); out=[]; counts=Counter()
    for row in part:
        r=dict(row); oid=str(r.get("overture_id") or ""); p=places.get(oid)
        if p:
            ovaddr=place_address(p.get("addresses")); haddr=str(r.get("hub_address") or ""); pc=str(r.get("hub_postalcode") or "")
            ht=base.tokens(haddr); ot=base.tokens(ovaddr); ao=len(ht&ot)/max(1,len(ht)) if ht else 0.0
            raw=json.dumps(p.get("addresses"),ensure_ascii=False,default=str); pm=bool(pc and re.search(r"(?<!\d)"+re.escape(pc)+r"(?!\d)",raw))
            r.update({"overture_resolved":True,"overture_address":ovaddr,"overture_addresses":raw,"address_overlap":round(ao,3),"postcode_match":pm,"phone_exact":False,"overture_operating_status":base.txt(p.get("operating_status")),"overture_current_confidence":base.txt(p.get("confidence"))})
            counts["identity_refreshed"]+=1
        else:
            r.update({"overture_resolved":False,"address_overlap":0.0,"postcode_match":False,"phone_exact":False})
            counts["identity_missing"]+=1
        r["verification_status"]="PENDING_SEARCH_VERIFY"; r["needs_gpt_review"]=True; out.append(r)
    d=Path(a.outdir); d.mkdir(parents=True,exist_ok=True)
    (d/"records.jsonl").write_text("".join(json.dumps(x,ensure_ascii=False,sort_keys=True,default=str)+"\n" for x in out),encoding="utf-8")
    metrics={"schema_version":1,"status":"completed","worker_index":a.worker_index,"worker_count":a.worker_count,"records_materialized":len(out),"review_candidates":len(out),"identity_refresh":dict(counts),"elapsed_seconds":round(time.time()-z,2)}
    (d/"metrics.json").write_text(json.dumps(metrics,indent=2)+"\n",encoding="utf-8"); (d/"checkpoint.json").write_text(json.dumps({"status":"completed","worker_index":a.worker_index},indent=2)+"\n",encoding="utf-8")
    print("GWS_PENDING_VERIFY_WORKER="+json.dumps(metrics,separators=(",",":")))

if __name__=="__main__": main()
