#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

SOUTH=("Uccle","Ixelles","Saint-Gilles","Forest","Auderghem","Watermael-Boitsfort")
HARD_TERMINAL_SEARCH_STATUSES={"HIGH","REJECT","DUPLICATE","ERROR_HARD"}
SOFT_SEARCH_STATUSES={"MEDIUM","UNCERTAIN"}


def load(path,default):
    try: return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception: return default


def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--outdir",default="results/gws_verify_plan"); ap.add_argument("--max-workers",type=int,default=6); ap.add_argument("--per-worker",type=int,default=70); a=ap.parse_args()
    pending=load("gpt/gws_pending_batches.json",{"batches":[]})
    index=load("state/gws_verify_index.json",{"records":{}}).get("records",{})
    semantic=load("state/gws_semantic_index.json",{"records":{}}).get("records",{})
    latest={}
    for batch in pending.get("batches") or []:
        if batch.get("status")!="pending" or not batch.get("batch"): continue
        p=Path(str(batch["batch"]))
        if not p.exists(): continue
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip(): continue
            r=json.loads(line); key=str(r.get("record_key") or "")
            if not key: continue
            r["source_batch"]=str(p); latest[key]=r

    rows=[]
    suppressed_terminal=0; retryable_or_pending=0; semantic_rechecks=0; soft_waiting_semantic=0
    semantic_candidates_forwarded=0
    for key,r in latest.items():
        prior=index.get(key) or {}; fp=str(r.get("fingerprint") or "")
        prior_status=str(prior.get("verification_status") or "").strip().upper()
        same=prior.get("source_fingerprint")==fp

        if same and prior_status in HARD_TERMINAL_SEARCH_STATUSES:
            suppressed_terminal+=1
            continue

        semantic_recheck=False
        if same and prior_status in SOFT_SEARCH_STATUSES:
            sem=semantic.get(key) or {}
            sem_fp=str(sem.get("semantic_fingerprint") or "")
            sem_status=str(sem.get("resolution_status") or "").upper()
            prior_sem_fp=str(prior.get("semantic_resolution_fingerprint") or "")
            prior_attempt=int(prior.get("semantic_resolution_attempt") or 0)
            # Qwen/GPT semantic stage is shadow-only. A QUEUED semantic verdict
            # authorizes exactly one new strict-search pass for that semantic
            # fingerprint; it never directly changes HIGH/REJECT.
            if sem_status=="QUEUED" and sem_fp and sem_fp!=prior_sem_fp and prior_attempt<2:
                semantic_recheck=True
                r=dict(r)
                r["semantic_resolution"]=True
                r["semantic_resolution_fingerprint"]=sem_fp
                r["semantic_resolution_route"]=str(sem.get("resolution_route") or "GPT_SEARCH_REVIEW")
                r["semantic_resolution_attempt"]=prior_attempt+1
                r["semantic_shadow_decision"]=sem.get("decision")
                r["semantic_shadow_confidence"]=sem.get("confidence")
                r["semantic_shadow_website_state"]=sem.get("website_state")
                # Preserve the expensive Search->Qwen evidence into strict. The
                # verifier may use this candidate only as an ownership-rejection
                # accelerator. It can never certify no-website/HIGH by itself.
                r["semantic_candidate_url"]=str(sem.get("candidate_url") or "")
                r["semantic_candidate_host_class"]=str(sem.get("candidate_host_class") or "")
                r["semantic_ownership_decision"]=str(sem.get("decision") or "")
                if r["semantic_candidate_url"]:
                    semantic_candidates_forwarded+=1
                semantic_rechecks+=1
            else:
                soft_waiting_semantic+=1
                continue

        if prior_status in {"PENDING_SEARCH_VERIFY","ERROR_RETRYABLE","SEARCH_INCOMPLETE",""}:
            retryable_or_pending+=1
        if r.get("outcome")=="REJECT" and not semantic_recheck: continue
        rows.append(r)

    # Resolve South Brussels first; within each geography prioritize semantic
    # rechecks so already-expensive UNCERTAIN records actually converge.
    rows.sort(key=lambda r:(
        0 if str(r.get("territory") or "") in SOUTH else 1,
        0 if r.get("semantic_resolution") else 1,
        str(r.get("territory") or ""),
        str(r.get("hub_name") or ""),
    ))

    eligible_total=len(rows)
    per_worker=max(1,int(a.per_worker))
    workers=min(max(0,int(a.max_workers)), math.ceil(eligible_total/per_worker)) if rows else 0
    selected=rows[:workers*per_worker] if workers else []

    out=Path(a.outdir); out.mkdir(parents=True,exist_ok=True)
    (out/"pending.jsonl").write_text("".join(json.dumps(x,ensure_ascii=False,sort_keys=True)+"\n" for x in selected),encoding="utf-8")
    matrix={"include":[{"worker_index":i,"worker_count":workers} for i in range(workers)]}
    plan={
        "schema_version":5,
        "eligible":eligible_total,
        "selected":len(selected),
        "deferred":max(0,eligible_total-len(selected)),
        "workers":workers,
        "per_worker_target":per_worker,
        "suppressed_terminal":suppressed_terminal,
        "retryable_or_pending_seen":retryable_or_pending,
        "semantic_rechecks_eligible":semantic_rechecks,
        "semantic_rechecks_selected":sum(1 for x in selected if x.get("semantic_resolution")),
        "semantic_candidates_forwarded":sum(1 for x in selected if x.get("semantic_candidate_url")),
        "semantic_candidates_available":semantic_candidates_forwarded,
        "soft_waiting_semantic":soft_waiting_semantic,
        "south_priority":list(SOUTH),
        "matrix":matrix,
    }
    (out/"plan.json").write_text(json.dumps(plan,indent=2)+"\n",encoding="utf-8")
    gh=Path(__import__('os').environ.get("GITHUB_OUTPUT","")) if __import__('os').environ.get("GITHUB_OUTPUT") else None
    if gh:
        with gh.open("a",encoding="utf-8") as f:
            f.write(f"eligible={eligible_total}\nworkers={workers}\nmatrix={json.dumps(matrix,separators=(',',':'))}\n")
    print("GWS_PENDING_VERIFY_PLAN="+json.dumps(plan,separators=(",",":")))

if __name__=="__main__": main()
