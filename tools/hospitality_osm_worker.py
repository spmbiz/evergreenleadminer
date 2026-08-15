#!/usr/bin/env python3
"""Production wrapper for one approved Geofabrik OSM hospitality extract.

Uses the same canonical-domain prefilter and live/recovery gates as Overture.
One worker summary covers both the direct-email and website-only recovery paths;
the canonical aggregate still sees the individual v6_live_ready.csv files.
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import fleet_runtime as fr

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/osm_geofabrik_sources.json"


def source_policy(extract_id: str) -> dict:
    cfg = fr.load_json(CONFIG, {})
    for row in cfg.get("extracts") or []:
        if row.get("extract_id") == extract_id:
            return row
    raise RuntimeError(f"Geofabrik extract not in policy: {extract_id}")


def run(cmd):
    p = subprocess.run(cmd, cwd=ROOT, text=True)
    if p.returncode:
        raise RuntimeError(f"exit {p.returncode}: {' '.join(cmd)}")


def reason_rates(paths):
    reasons = {}
    for path in paths:
        if not path.exists():
            continue
        with path.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                reason = row.get("live_reason") or ""
                reasons[reason] = reasons.get(reason, 0) + 1
    checked = sum(reasons.values())
    r429 = reasons.get("HTTP_429", 0) / checked if checked else 0
    timeouts = sum(v for k, v in reasons.items() if "TIMEOUT" in k)
    errors = sum(v for k, v in reasons.items() if k.startswith("NETWORK_") or k.startswith("HTTP_5"))
    return reasons, round(r429, 5), round(timeouts / checked if checked else 0, 5), round(errors / checked if checked else 0, 5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="github")
    ap.add_argument("--cycle-id", required=True)
    ap.add_argument("--extract-id", required=True)
    ap.add_argument("--expected-release", default="")
    ap.add_argument("--canonical-domains", default="")
    ap.add_argument("--local-workers", type=int, default=32)
    ap.add_argument("--contact-workers", type=int, default=16)
    ap.add_argument("--outdir", required=True)
    a = ap.parse_args()

    cfg = source_policy(a.extract_id)
    if not cfg.get("production_enabled"):
        raise SystemExit(f"Geofabrik extract not production-enabled: {a.extract_id}")

    root = Path(a.outdir)
    source = root / "source"
    fast = root / "fast"
    recovery = root / "recovery"
    for d in (source, fast, recovery):
        d.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    status, error = "success", ""
    try:
        run([
            sys.executable, "tools/geofabrik_osm_hospitality.py",
            "--extract-id", a.extract_id,
            "--iso2", str(cfg.get("iso2") or ""),
            "--country", str(cfg.get("country") or ""),
            "--outdir", str(source),
            "--max-download-mb", str(int(cfg.get("max_download_mb") or 200)),
        ])
        shutil.copy2(source / "v6_fast_ready.csv", fast / "v6_fast_ready.csv")
        shutil.copy2(source / "v6_recovery_candidates.csv", recovery / "v6_recovery_candidates.csv")
        if a.canonical_domains and Path(a.canonical_domains).exists():
            run([sys.executable, "tools/filter_canonical_domains.py", "--input", str(fast / "v6_fast_ready.csv"), "--domains", a.canonical_domains, "--stats", str(fast / "canonical_prefilter.json")])
            run([sys.executable, "tools/filter_canonical_domains.py", "--input", str(recovery / "v6_recovery_candidates.csv"), "--domains", a.canonical_domains, "--stats", str(recovery / "canonical_prefilter.json")])
        run([sys.executable, "tools/v6_live_verify.py", "--input", str(fast / "v6_fast_ready.csv"), "--outdir", str(fast), "--workers", str(a.local_workers), "--timeout", "8"])
        run([sys.executable, "tools/v6_public_contact_enrich.py", "--input", str(recovery / "v6_recovery_candidates.csv"), "--outdir", str(recovery), "--workers", str(a.contact_workers), "--timeout", "8", "--max-pages", "3", "--max-bytes", "700000"])
        run([sys.executable, "tools/v6_live_verify.py", "--input", str(recovery / "v6_fast_ready.csv"), "--outdir", str(recovery), "--workers", str(a.local_workers), "--timeout", "8"])
    except Exception as e:
        status = "failed_retryable"
        error = f"{type(e).__name__}: {e}"

    src = fr.load_json(source / "osm_summary.json", {})
    fl = fr.load_json(fast / "v6_live_summary.json", {})
    rl = fr.load_json(recovery / "v6_live_summary.json", {})
    rec = fr.load_json(recovery / "v6_contact_recovery_summary.json", {})
    fpf = fr.load_json(fast / "canonical_prefilter.json", {})
    rpf = fr.load_json(recovery / "canonical_prefilter.json", {})
    reasons, r429, timeout_rate, error_rate = reason_rates([fast / "v6_live_verified.csv", recovery / "v6_live_verified.csv"])
    release = a.expected_release or "latest"
    summary = {
        "provider":a.provider,
        "cycle_id":a.cycle_id,
        "lane":"osm_geofabrik",
        "task_type":"osm_geofabrik",
        "extract_id":a.extract_id,
        "shard":{
            "name":f"OSM::{a.extract_id}",
            "country":str(cfg.get("country") or ""),
            "region":f"OSM::{a.extract_id}",
            "bbox":f"osm:{a.extract_id}",
            "release":release,
        },
        "status":status,
        "error":error,
        "local_workers":a.local_workers,
        "elapsed_seconds":round(time.time()-t0, 2),
        "raw_site_email_rows":int(src.get("hospitality_domains") or 0),
        "canonical_prefilter_rejected":int(fpf.get("canonical_domain_rejected") or 0)+int(rpf.get("canonical_domain_rejected") or 0),
        "fast_ready":int(fl.get("input_fast_ready") or 0)+int(rl.get("input_fast_ready") or 0),
        "recovered_public_emails":int(rec.get("recovered_public_emails") or 0),
        "live_high":int(fl.get("live_high") or 0)+int(rl.get("live_high") or 0),
        "live_medium":int(fl.get("live_medium") or 0)+int(rl.get("live_medium") or 0),
        "live_ready":int(fl.get("live_ready") or 0)+int(rl.get("live_ready") or 0),
        "instagram_found":int(fl.get("instagram_found") or 0)+int(rl.get("instagram_found") or 0),
        "http_429_rate":r429,
        "timeout_rate":timeout_rate,
        "error_rate":error_rate,
        "live_reasons":reasons,
        "pbf_url":src.get("pbf_url") or str(cfg.get("pbf_url") or ""),
    }
    fr.write_json(root / "worker_summary.json", summary)
    print(json.dumps(summary, indent=2))
    if status != "success":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
