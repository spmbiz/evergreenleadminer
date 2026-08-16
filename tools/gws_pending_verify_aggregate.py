#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path


def load(path,default):
    try: return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception: return default

def dump(path,obj):
    p=Path(path); p.parent.mkdir(parents=True,exist_ok=True); p.write_text(json.dumps(obj,ensure_ascii=False,indent=2,sort_keys=True)+"\n",encoding="utf-8")
def append(path,rows):
    if not rows: return
    p=Path(path); p.parent.mkdir(parents=True,exist_ok=True)
    with p.open("a",encoding="utf-8") as f:
        for r in rows: f.write(json.dumps(r,ensure_ascii=False,sort_keys=True,default=str)+"\n")
def now(): return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def worker_dirs(root): return sorted({p.parent for p in Path(root).rglob("records.jsonl")})


def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--root",default="results/gws_verify_shards"); a=ap.parse_args()
    index=load("state/gws_verify_index.json",{"schema_version":1,"records":{}}); records=index.setdefault("records",{})
    rows=[]
    for d in worker_dirs(a.root):
        for line in (d/"records.jsonl").read_text(encoding="utf-8").splitlines():
            if line.strip(): rows.append(json.loads(line))
    ts=now(); date=ts[:10]; ready=[]; highs=[]; retryable=0; rejects=0
    for r in rows:
        key=str(r.get("record_key") or "")
        if not key: continue
        status=str(r.get("verification_status") or r.get("outcome") or "")
        entry={"source_fingerprint":str(r.get("fingerprint") or ""),"verification_status":status,"outcome":r.get("outcome"),"reason":r.get("reason"),"verification_provider":r.get("verification_provider"),"certificate_digest":r.get("certificate_digest"),"owned_website":r.get("owned_website"),"last_verified":ts,"source_batch":r.get("source_batch")}
        records[key]=entry
        if status=="ERROR_RETRYABLE": retryable+=1; continue
        if r.get("outcome")=="REJECT": rejects+=1; continue
        ready.append(r)
        if r.get("outcome")=="HIGH" and r.get("reason")=="VERIFIED_NO_WEBSITE": highs.append(r)
    append(Path("data/gws/verification")/f"{date}.jsonl",rows)

    stamp=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    verified_pending=load("gpt/gws_verified_pending.json",{"schema_version":1,"batches":[],"pending_records":0})
    if ready:
        path=Path("gpt/gws_verified_review")/f"{stamp}.jsonl"; append(path,ready)
        verified_pending.setdefault("batches",[]).append({"batch":str(path),"created_at":ts,"records":len(ready),"strict_high":len(highs),"status":"pending_canonical_review","reviewed_at":None})
    verified_pending["pending_records"]=sum(int(x.get("records") or 0) for x in verified_pending.get("batches",[]) if x.get("status")=="pending_canonical_review")
    dump("gpt/gws_verified_pending.json",verified_pending)

    pending=load("gpt/gws_pending_batches.json",{"schema_version":1,"batches":[]})
    total_remaining=0
    for b in pending.get("batches") or []:
        if not b.get("batch"): continue
        p=Path(str(b["batch"])); remaining=0; total=0
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                if not line.strip(): continue
                r=json.loads(line); key=str(r.get("record_key") or ""); total+=1; state=records.get(key) or {}
                same=state.get("source_fingerprint")==str(r.get("fingerprint") or "")
                resolved=same and state.get("verification_status") not in {None,"","ERROR_RETRYABLE","SEARCH_INCOMPLETE","PENDING_SEARCH_VERIFY"}
                if not resolved: remaining+=1
        b["verification_remaining"]=remaining; b["verification_total"]=total
        if total and remaining==0 and b.get("status")=="pending":
            b["status"]="verified_search_pass"; b["reviewed_at"]=ts
        total_remaining+=remaining
    pending["pending_records"]=total_remaining; dump("gpt/gws_pending_batches.json",pending)
    dump("state/gws_verify_index.json",index)
    metrics={"schema_version":1,"at":ts,"attempted":len(rows),"ready_for_canonical_review":len(ready),"strict_high_verified_no_website":len(highs),"rejected_owned_or_other":rejects,"retryable":retryable,"remaining_source_backlog":total_remaining,"canonical_high_persisted_here":0,"note":"HIGH is pre-canonical. Canonical MASTER dedupe/persist/readback remains downstream."}
    dump("metrics/gws_verify_latest.json",metrics); append("metrics/gws_verify_history.jsonl",[metrics]); print("GWS_PENDING_VERIFY_AGG="+json.dumps(metrics,separators=(",",":")))

if __name__=="__main__": main()
