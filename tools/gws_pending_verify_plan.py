#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

SOUTH=("Uccle","Ixelles","Saint-Gilles","Forest","Auderghem","Watermael-Boitsfort")


def load(path,default):
    try: return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception: return default


def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--outdir",default="results/gws_verify_plan"); ap.add_argument("--max-workers",type=int,default=6); ap.add_argument("--per-worker",type=int,default=70); a=ap.parse_args()
    pending=load("gpt/gws_pending_batches.json",{"batches":[]}); index=load("state/gws_verify_index.json",{"records":{}}).get("records",{})
    latest={}
    for batch in pending.get("batches") or []:
        if batch.get("status")!="pending" or not batch.get("batch"): continue
        p=Path(str(batch["batch"]));
        if not p.exists(): continue
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip(): continue
            r=json.loads(line); key=str(r.get("record_key") or "")
            if not key: continue
            r["source_batch"]=str(p); latest[key]=r
    rows=[]
    for key,r in latest.items():
        prior=index.get(key) or {}; fp=str(r.get("fingerprint") or "")
        if prior.get("source_fingerprint")==fp and prior.get("verification_status") not in {"ERROR_RETRYABLE","SEARCH_INCOMPLETE"}: continue
        if r.get("outcome")=="REJECT": continue
        rows.append(r)
    rows.sort(key=lambda r:(0 if str(r.get("territory") or "") in SOUTH else 1, str(r.get("territory") or ""), str(r.get("hub_name") or "")))
    out=Path(a.outdir); out.mkdir(parents=True,exist_ok=True)
    (out/"pending.jsonl").write_text("".join(json.dumps(x,ensure_ascii=False,sort_keys=True)+"\n" for x in rows),encoding="utf-8")
    workers=min(max(0,a.max_workers), math.ceil(len(rows)/max(1,a.per_worker))) if rows else 0
    matrix={"include":[{"worker_index":i,"worker_count":workers} for i in range(workers)]}
    plan={"schema_version":1,"eligible":len(rows),"workers":workers,"per_worker_target":a.per_worker,"south_priority":list(SOUTH),"matrix":matrix}
    (out/"plan.json").write_text(json.dumps(plan,indent=2)+"\n",encoding="utf-8")
    gh=Path(__import__('os').environ.get("GITHUB_OUTPUT","")) if __import__('os').environ.get("GITHUB_OUTPUT") else None
    if gh:
        with gh.open("a",encoding="utf-8") as f:
            f.write(f"eligible={len(rows)}\nworkers={workers}\nmatrix={json.dumps(matrix,separators=(',',':'))}\n")
    print("GWS_PENDING_VERIFY_PLAN="+json.dumps(plan,separators=(",",":")))

if __name__=="__main__": main()
