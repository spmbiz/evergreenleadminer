#!/usr/bin/env python3
"""Production wrapper for fresh SearchFabric Hospitality discovery.

This source reuses the exact V1 public-contact recovery and permissive live gate.
The canonical aggregate remains the only writer. DDGS is installed only inside
a fresh-search worker so ordinary Overture/ATP/OSM workers keep their startup
cost unchanged.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import fleet_runtime as fr

ROOT = Path(__file__).resolve().parents[1]


def run(cmd):
    p = subprocess.run(cmd, cwd=ROOT, text=True)
    if p.returncode:
        raise RuntimeError(f"exit {p.returncode}: {' '.join(cmd)}")


def ensure_ddgs():
    try:
        import ddgs  # noqa: F401
        return
    except Exception:
        pass
    run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "ddgs"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="github")
    ap.add_argument("--cycle-id", required=True)
    ap.add_argument("--cursor", type=int, required=True)
    ap.add_argument("--max-queries", type=int, default=30)
    ap.add_argument("--canonical-domains", default="")
    ap.add_argument("--local-workers", type=int, default=32)
    ap.add_argument("--contact-workers", type=int, default=24)
    ap.add_argument("--outdir", required=True)
    a = ap.parse_args()

    root = Path(a.outdir)
    source = root / "source"
    recovery = root / "recovery"
    source.mkdir(parents=True, exist_ok=True)
    recovery.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    status, error = "success", ""
    try:
        ensure_ddgs()
        run([
            sys.executable, "tools/hospitality_fresh_search_source.py",
            "--canonical-domains", a.canonical_domains,
            "--outdir", str(source),
            "--cursor", str(a.cursor),
            "--max-queries", str(a.max_queries),
        ])
        import shutil
        shutil.copy2(source / "v6_recovery_candidates.csv", recovery / "v6_recovery_candidates.csv")
        run([
            sys.executable, "tools/v6_public_contact_enrich.py",
            "--input", str(recovery / "v6_recovery_candidates.csv"),
            "--outdir", str(recovery),
            "--workers", str(a.contact_workers),
            "--timeout", "8",
            "--max-pages", "3",
            "--max-bytes", "700000",
        ])
        run([
            sys.executable, "tools/promote_contact_ready.py",
            "--input", str(recovery / "v6_recovery_enriched.csv"),
            "--output", str(recovery / "v6_fast_ready.csv"),
            "--summary", str(recovery / "v6_contact_ready_summary.json"),
        ])
        run([
            sys.executable, "tools/v6_live_verify.py",
            "--input", str(recovery / "v6_fast_ready.csv"),
            "--outdir", str(recovery),
            "--workers", str(a.local_workers),
            "--timeout", "8",
        ])
    except Exception as exc:
        status = "failed_retryable"
        error = f"{type(exc).__name__}: {exc}"

    src = fr.load_json(source / "fresh_search_summary.json", {})
    rec = fr.load_json(recovery / "v6_contact_recovery_summary.json", {})
    ready = fr.load_json(recovery / "v6_contact_ready_summary.json", {})
    live = fr.load_json(recovery / "v6_live_summary.json", {})
    summary = {
        "provider":a.provider,
        "cycle_id":a.cycle_id,
        "lane":"fresh_search",
        "task_type":"fresh_search",
        "shard":{
            "name":f"FRESH_SEARCH::{a.cursor}",
            "country":"MULTI",
            "region":f"FRESH_SEARCH::{a.cursor}",
            "bbox":f"fresh-search:{a.cursor}",
            "release":"daily",
        },
        "status":status,
        "error":error,
        "local_workers":a.local_workers,
        "elapsed_seconds":round(time.time()-t0,2),
        "raw_site_email_rows":int(src.get("raw_search_results") or 0),
        "canonical_prefilter_rejected":int(src.get("canonical_known_rejected_early") or 0),
        "fresh_candidate_domains":int(src.get("canonical_unseen_candidate_domains") or 0),
        "recovery_candidates":int(src.get("canonical_unseen_candidate_domains") or 0),
        "recovered_public_emails":int(rec.get("recovered_public_emails") or 0),
        "contact_ready":int(ready.get("contact_ready") or 0),
        "social_or_contact_without_email":int(ready.get("social_or_contact_without_email") or 0),
        "fast_ready":int(live.get("input_fast_ready") or 0),
        "live_high":int(live.get("live_high") or 0),
        "live_medium":int(live.get("live_medium") or 0),
        "live_ready":int(live.get("live_ready") or 0),
        "instagram_found":int(rec.get("instagram_found") or live.get("instagram_found") or 0),
        "facebook_found":int(rec.get("facebook_found") or 0),
        "http_429_rate":0.0,
        "timeout_rate":0.0,
        "error_rate":0.0 if status == "success" else 1.0,
        "search_cursor":a.cursor,
        "search_queries":a.max_queries,
    }
    fr.write_json(root / "worker_summary.json", summary)
    print(json.dumps(summary, indent=2))
    if status != "success":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
