#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path

import gws_ownership_gate as ownership
import gws_scope_gate as scope


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


def ownership_pass_for(row):
    reason=str(row.get("reason") or "")
    if "PASS2" in reason: return row.get("web_pass2") or {}
    return row.get("web_pass1") or row.get("web_pass2") or {}


def reject_explicit_nonbusiness_scope(row):
    r=dict(row)
    assessment=scope.assess(r)
    r["aggregate_scope_assessment"]=assessment
    if assessment.get("in_scope"):
        return r, False
    previous={
        "outcome":r.get("outcome"),
        "reason":r.get("reason"),
        "verification_status":r.get("verification_status"),
        "certificate_digest":r.get("certificate_digest"),
    }
    r["scope_guard_previous"]=previous
    r["outcome"]="REJECT"
    r["reason"]="SCOPE_NON_BUSINESS_SOURCE_CATEGORY"
    r["verification_status"]="REJECT"
    r["needs_gpt_review"]=False
    r["owned_website"]=""
    cert=dict(r.get("certificate") or {})
    cert["verified"]=False
    cert["scope_rejected"]=True
    cert["scope_reject_reason"]=assessment.get("reason")
    r["certificate"]=cert
    r["certificate_digest"]=""
    return r, True


def quarantine_unproven_reject(row):
    r=dict(row)
    if r.get("outcome") != "REJECT" or "OWNED_SITE" not in str(r.get("reason") or ""):
        return r, False
    assessment=ownership.assess(r, ownership_pass_for(r))
    r["aggregate_ownership_assessment"]=assessment
    if assessment.get("confident"):
        r["ownership_guard_passed"]=True
        return r, False

    previous={
        "outcome":r.get("outcome"), "reason":r.get("reason"),
        "owned_website":r.get("owned_website"), "verification_status":r.get("verification_status"),
    }
    r["outcome"]="UNCERTAIN"
    r["reason"]="OWNERSHIP_REJECT_QUARANTINED"
    r["verification_status"]="UNCERTAIN"
    r["needs_gpt_review"]=True
    r["ownership_guard_passed"]=False
    r["ownership_quarantine_previous"]=previous
    r["ownership_ambiguous_candidates"]=[assessment]
    r["owned_website"]=""
    cert=dict(r.get("certificate") or {})
    cert["verified"]=False
    cert["ownership_quarantined"]=True
    r["certificate"]=cert
    r["certificate_digest"]=""
    return r, True


def current_run_quarantine() -> dict:
    run_id=str(os.environ.get("GITHUB_RUN_ID") or "").strip()
    if not run_id:
        return {}
    q=load(Path("state/gws_strict_run_quarantine")/f"{run_id}.json",{})
    if q and q.get("canonical_eligible") is False:
        return q
    return {}


def quarantine_run_high(row, run_quarantine):
    r=dict(row)
    if not run_quarantine:
        return r, False
    if str(r.get("verification_status") or "").upper()!="HIGH":
        return r, False
    if str(r.get("reason") or "").upper()!="VERIFIED_NO_WEBSITE":
        return r, False
    previous={
        "outcome":r.get("outcome"),
        "reason":r.get("reason"),
        "verification_status":r.get("verification_status"),
        "certificate_digest":r.get("certificate_digest"),
        "certificate":r.get("certificate"),
    }
    r["run_quarantine_previous"]=previous
    r["run_quarantine_id"]=str(run_quarantine.get("strict_workflow_run_id") or os.environ.get("GITHUB_RUN_ID") or "")
    r["run_quarantine_status"]=str(run_quarantine.get("status") or "QUARANTINED_REQUIRE_LATEST_MAIN_REVERIFY")
    r["outcome"]="REVIEW"
    r["reason"]="STRICT_RUN_QUARANTINED_REVERIFY_REQUIRED"
    r["verification_status"]="ERROR_RETRYABLE"
    r["needs_gpt_review"]=True
    cert=dict(r.get("certificate") or {})
    cert["verified"]=False
    cert["run_quarantined"]=True
    cert["superseded_certificate_digest"]=str(r.get("certificate_digest") or "")
    r["certificate"]=cert
    r["certificate_digest"]=""
    return r, True


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--root",default="results/gws_verify_shards")
    # Compatibility with the fleet workflow. The plan directory is transport-only;
    # aggregation intentionally derives canonical state from worker result shards.
    ap.add_argument("--plan-dir",default=None)
    a=ap.parse_args()
    index=load("state/gws_verify_index.json",{"schema_version":1,"records":{}}); records=index.setdefault("records",{})
    semantic_index=load("state/gws_semantic_index.json",{"schema_version":1,"records":{}}); semantic_records=semantic_index.setdefault("records",{})
    raw_rows=[]
    for d in worker_dirs(a.root):
        for line in (d/"records.jsonl").read_text(encoding="utf-8").splitlines():
            if line.strip(): raw_rows.append(json.loads(line))

    run_quarantine=current_run_quarantine()
    rows=[]; quarantined=0; run_highs_quarantined=0; scope_rejected=0
    for raw in raw_rows:
        clean, sq=reject_explicit_nonbusiness_scope(raw)
        clean, q=quarantine_unproven_reject(clean)
        clean, rq=quarantine_run_high(clean, run_quarantine)
        rows.append(clean)
        scope_rejected += int(sq)
        quarantined += int(q)
        run_highs_quarantined += int(rq)

    ts=now(); date=ts[:10]; ready=[]; highs=[]; retryable=0; rejects=0
    semantic_attempted=0; semantic_resolved=0; semantic_hard=[]; semantic_retryable=0
    for r in rows:
        key=str(r.get("record_key") or "")
        if not key: continue
        status=str(r.get("verification_status") or r.get("outcome") or "")
        sem_fp=str(r.get("semantic_resolution_fingerprint") or "")
        sem_attempt=int(r.get("semantic_resolution_attempt") or 0)
        sem_route=str(r.get("semantic_resolution_route") or "")
        entry={
            "source_fingerprint":str(r.get("fingerprint") or ""),"verification_status":status,"outcome":r.get("outcome"),
            "reason":r.get("reason"),"verification_provider":r.get("verification_provider"),"certificate_digest":r.get("certificate_digest"),
            "owned_website":r.get("owned_website"),"last_verified":ts,"source_batch":r.get("source_batch"),
            "semantic_resolution_fingerprint":sem_fp,"semantic_resolution_attempt":sem_attempt,"semantic_resolution_route":sem_route,
            "run_quarantine_id":r.get("run_quarantine_id"),"run_quarantine_status":r.get("run_quarantine_status"),
            "scope_reason":(r.get("aggregate_scope_assessment") or {}).get("reason"),
        }
        records[key]=entry

        if r.get("semantic_resolution"):
            semantic_attempted+=1
            sem=semantic_records.setdefault(key,{})
            sem["strict_resolution_attempted_at"]=ts
            sem["strict_resolution_fingerprint"]=sem_fp
            sem["strict_resolution_attempt"]=sem_attempt
            sem["strict_resolution_route"]=sem_route
            sem["strict_resolution_outcome"]=r.get("outcome")
            sem["strict_resolution_reason"]=r.get("reason")
            if status in {"HIGH","REJECT","DUPLICATE"}:
                sem["resolution_status"]="RESOLVED_STRICT"
                sem["resolution_final_status"]=status
                semantic_resolved+=1
            elif status in {"ERROR_RETRYABLE","SEARCH_INCOMPLETE"}:
                sem["resolution_status"]="STRICT_RETRYABLE"
                semantic_retryable+=1
            elif status in {"MEDIUM","UNCERTAIN"}:
                sem["resolution_status"]="HARD_REVIEW_REQUIRED"
                sem["resolution_final_status"]="UNCERTAIN"
                hard=dict(r)
                hard["hard_review_reason"]="SEMANTIC_GUIDED_STRICT_RECHECK_INCONCLUSIVE"
                hard["hard_review_queued_at"]=ts
                semantic_hard.append(hard)

        if status=="ERROR_RETRYABLE": retryable+=1; continue
        if r.get("outcome")=="REJECT": rejects+=1; continue
        ready.append(r)
        if r.get("outcome")=="HIGH" and r.get("reason")=="VERIFIED_NO_WEBSITE": highs.append(r)
    append(Path("data/gws/verification")/f"{date}.jsonl",rows)

    stamp=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    if semantic_hard:
        append(Path("gpt/gws_semantic_hard_review")/f"{stamp}.jsonl",semantic_hard)

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
    dump("state/gws_semantic_index.json",semantic_index)
    metrics={
        "schema_version":5,"at":ts,"attempted":len(rows),"ready_for_canonical_review":len(ready),
        "strict_high_verified_no_website":len(highs),"rejected_owned_or_other":rejects,"scope_nonbusiness_rejected":scope_rejected,
        "ownership_rejects_quarantined":quarantined,
        "run_highs_quarantined_for_reverify":run_highs_quarantined,
        "strict_run_quarantine_id":str(run_quarantine.get("strict_workflow_run_id") or "") if run_quarantine else "",
        "retryable":retryable,"remaining_source_backlog":total_remaining,"canonical_high_persisted_here":0,
        "semantic_resolution_attempted":semantic_attempted,"semantic_resolution_resolved":semantic_resolved,
        "semantic_resolution_retryable":semantic_retryable,"semantic_hard_review_added":len(semantic_hard),
        "note":"HIGH is pre-canonical. Explicit public/non-business source categories are rejected before HIGH persistence. Quarantined-run HIGHs are forced non-terminal. Semantic/Qwen is shadow-only."
    }
    dump("metrics/gws_verify_latest.json",metrics); append("metrics/gws_verify_history.jsonl",[metrics]); print("GWS_PENDING_VERIFY_AGG="+json.dumps(metrics,separators=(",",":")))

if __name__=="__main__": main()
