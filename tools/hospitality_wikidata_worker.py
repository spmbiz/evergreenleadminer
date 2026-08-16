#!/usr/bin/env python3
"""Worker adapter for the read-only Wikidata Hospitality source."""
from __future__ import annotations

import argparse
import gzip
import json
import subprocess
import sys
import time
from pathlib import Path

import fleet_runtime as fr

ROOT = Path(__file__).resolve().parents[1]
MIN_CANONICAL_DOMAINS = 10_000


def run(cmd: list[str]) -> None:
    p = subprocess.run(cmd, cwd=ROOT, text=True)
    if p.returncode:
        raise RuntimeError(f"exit {p.returncode}: {' '.join(cmd)}")


def snapshot_count(path: str) -> int:
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return 0
    opener = gzip.open if p.suffix == ".gz" else open
    count = 0
    with opener(p, "rt", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                count += 1
    return count


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="github")
    ap.add_argument("--cycle-id", required=True)
    ap.add_argument("--country-qid", required=True)
    ap.add_argument("--country", required=True)
    ap.add_argument("--country-code", default="")
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--canonical-domains", required=True)
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
    count = 0
    try:
        count = snapshot_count(a.canonical_domains)
        if count < MIN_CANONICAL_DOMAINS:
            raise RuntimeError(f"canonical snapshot too small: {count}")
        run([
            sys.executable, "tools/hospitality_wikidata_source.py",
            "--country-qid", a.country_qid,
            "--country", a.country,
            "--country-code", a.country_code,
            "--offset", str(a.offset),
            "--limit", str(a.limit),
            "--canonical-domains", a.canonical_domains,
            "--outdir", str(source),
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

    src = fr.load_json(source / "wikidata_source_summary.json", {})
    rec = fr.load_json(recovery / "v6_contact_recovery_summary.json", {})
    ready = fr.load_json(recovery / "v6_contact_ready_summary.json", {})
    live = fr.load_json(recovery / "v6_live_summary.json", {})
    bbox = f"wikidata:{a.country_qid}:{a.offset}"
    region = f"WIKIDATA::{a.country_code or a.country_qid}::{a.offset}"
    summary = {
        "provider": a.provider,
        "cycle_id": a.cycle_id,
        "lane": "wikidata_hospitality",
        "task_type": "wikidata_hospitality",
        "shard": {
            "name": region,
            "country": a.country,
            "region": region,
            "bbox": bbox,
            "release": time.strftime("%Y-%m-%d", time.gmtime()),
            "lane": "wikidata_hospitality"
        },
        "country": a.country,
        "country_qid": a.country_qid,
        "offset": a.offset,
        "status": status,
        "error": error,
        "canonical_snapshot_domains": count,
        "local_workers": a.local_workers,
        "contact_workers": a.contact_workers,
        "elapsed_seconds": round(time.time() - t0, 2),
        "raw_site_email_rows": int(src.get("raw_bindings") or 0),
        "canonical_prefilter_rejected": int(src.get("canonical_known_rejected_early") or 0),
        "fresh_candidate_domains": int(src.get("canonical_unseen_candidate_domains") or 0),
        "recovery_candidates": int(src.get("canonical_unseen_candidate_domains") or 0),
        "recovered_public_emails": int(rec.get("recovered_public_emails") or 0),
        "contact_ready": int(ready.get("contact_ready") or 0),
        "social_or_contact_without_email": int(ready.get("social_or_contact_without_email") or 0),
        "fast_ready": int(live.get("input_fast_ready") or 0),
        "live_ready": int(live.get("live_ready") or 0),
        "instagram_found": int(rec.get("instagram_found") or live.get("instagram_found") or 0),
        "facebook_found": int(rec.get("facebook_found") or 0),
        "http_429_rate": 0.0,
        "timeout_rate": 0.0,
        "error_rate": 0.0 if status == "success" else 1.0,
        "contact_429_rate": 0.0,
        "contact_timeout_rate": 0.0,
        "contact_error_rate": 0.0,
        "worker_errors": 0 if status == "success" else 1,
    }
    fr.write_json(root / "worker_summary.json", summary)
    print(json.dumps(summary, indent=2))
    if status != "success":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
