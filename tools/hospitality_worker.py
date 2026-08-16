#!/usr/bin/env python3
"""Provider-neutral hospitality worker.

Supported lanes:
- fast_email: zero-HTTP Overture website+email discovery, canonical-domain
  prefilter, then live verification.
- site_recovery: Overture website-first discovery, canonical-domain prefilter,
  bounded first-party public contact recovery, then the same live gate.
- fresh_search: canonical-unseen premium/operator discovery through SearchFabric,
  followed by the same V1 contact recovery and permissive live gate.
- wikidata_hospitality: structured Wikidata hotel/resort entities with explicit
  official websites, then the same canonical prefilter/contact/live gate.
- batch64:<payload> bbox: runner-local queue of ordinary geo cells.

Final canonicalization remains the single writer.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

import fleet_runtime as fr


def prefilter(path: Path, domains: str, out: Path, label: str):
    if not domains or not Path(domains).exists():
        return
    fr.run([
        sys.executable,
        "tools/filter_canonical_domains.py",
        "--input", str(path),
        "--domains", domains,
        "--stats", str(out / f"canonical_prefilter_{label}.json"),
    ])


def worker(a):
    out = Path(a.outdir)
    out.mkdir(parents=True, exist_ok=True)

    if str(a.bbox).startswith("batch64:"):
        payload = str(a.bbox).split(":", 1)[1]
        fr.run([
            sys.executable,
            "tools/hospitality_geo_batch_worker.py",
            "--provider", a.provider,
            "--cycle-id", a.cycle_id,
            "--cells-b64", payload,
            "--canonical-domains", a.canonical_domains,
            "--outdir", str(out),
        ])
        return

    if a.lane == "fresh_search" or str(a.bbox).startswith("fresh-search:"):
        try:
            cursor = int(str(a.bbox).split(":", 1)[1])
        except Exception:
            cursor = 0
        fr.run([
            sys.executable,
            "tools/hospitality_fresh_search_worker.py",
            "--provider", a.provider,
            "--cycle-id", a.cycle_id,
            "--cursor", str(cursor),
            "--max-queries", str(max(1, int(a.max_rows or 30))),
            "--canonical-domains", a.canonical_domains,
            "--local-workers", str(a.local_workers),
            "--contact-workers", str(a.contact_workers),
            "--outdir", str(out),
        ])
        return

    if a.lane == "wikidata_hospitality" or str(a.bbox).startswith("wikidata:"):
        parts = str(a.bbox).split(":")
        qid = parts[1] if len(parts) > 1 else ""
        try:
            offset = int(parts[2]) if len(parts) > 2 else 0
        except Exception:
            offset = 0
        country_code = ""
        if str(a.region).startswith("WIKIDATA::"):
            bits = str(a.region).split("::")
            if len(bits) > 1:
                country_code = bits[1]
        fr.run([
            sys.executable,
            "tools/hospitality_wikidata_worker.py",
            "--provider", a.provider,
            "--cycle-id", a.cycle_id,
            "--country-qid", qid,
            "--country", a.country,
            "--country-code", country_code,
            "--offset", str(offset),
            "--limit", str(max(1, int(a.max_rows or 300))),
            "--canonical-domains", a.canonical_domains,
            "--local-workers", str(a.local_workers),
            "--contact-workers", str(a.contact_workers),
            "--outdir", str(out),
        ])
        return

    t0 = time.time()
    status = "success"
    err = ""
    try:
        if a.lane == "site_recovery":
            fr.run([
                sys.executable,
                "tools/overture_v6_site_recovery.py",
                f"--bbox={a.bbox}",
                "--country", a.country,
                "--region", a.region,
                "--outdir", str(out),
                "--release", a.release,
                "--max-rows", str(a.max_rows),
            ])
            prefilter(out / "v6_recovery_candidates.csv", a.canonical_domains, out, "recovery")
            fr.run([
                sys.executable,
                "tools/v6_public_contact_enrich.py",
                "--input", str(out / "v6_recovery_candidates.csv"),
                "--outdir", str(out),
                "--workers", str(a.contact_workers),
                "--timeout", str(a.contact_timeout),
                "--max-pages", str(a.contact_max_pages),
                "--max-bytes", str(a.contact_max_bytes),
            ])
            fr.run([
                sys.executable,
                "tools/promote_contact_ready.py",
                "--input", str(out / "v6_recovery_enriched.csv"),
                "--output", str(out / "v6_fast_ready.csv"),
                "--summary", str(out / "v6_contact_ready_summary.json"),
            ])
        else:
            fr.run([
                sys.executable,
                "tools/overture_v6_fastlane.py",
                f"--bbox={a.bbox}",
                "--country", a.country,
                "--region", a.region,
                "--outdir", str(out),
                "--release", a.release,
                "--max-rows", str(a.max_rows),
            ])
            prefilter(out / "v6_fast_ready.csv", a.canonical_domains, out, "fast")

        verifier = "tools/v6_live_verify_async.py" if a.verify_engine == "async" else "tools/v6_live_verify.py"
        cmd = [
            sys.executable,
            verifier,
            "--input", str(out / "v6_fast_ready.csv"),
            "--outdir", str(out),
            "--workers", str(a.local_workers),
            "--timeout", "7",
        ]
        if a.verify_engine == "async":
            cmd.extend(["--per-host", str(a.per_host)])
        fr.run(cmd)
    except Exception as e:
        status = "failed_retryable"
        err = f"{type(e).__name__}: {e}"

    fast = fr.load_json(out / "v6_fast_summary.json", {})
    recovery = fr.load_json(out / "v6_contact_recovery_summary.json", {})
    contact_ready = fr.load_json(out / "v6_contact_ready_summary.json", {})
    live = fr.load_json(out / "v6_live_summary.json", {})
    pf_fast = fr.load_json(out / "canonical_prefilter_fast.json", {})
    pf_recovery = fr.load_json(out / "canonical_prefilter_recovery.json", {})
    reasons = {}
    vp = out / "v6_live_verified.csv"
    if vp.exists():
        with vp.open(encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                reason = r.get("live_reason") or ""
                reasons[reason] = reasons.get(reason, 0) + 1

    checked = sum(reasons.values())
    rate429 = reasons.get("HTTP_429", 0) / checked if checked else 0
    timeouts = sum(v for k, v in reasons.items() if "TIMEOUT" in k)
    errors = sum(v for k, v in reasons.items() if k.startswith("NETWORK_") or k.startswith("HTTP_5"))
    recovery_reasons = recovery.get("reasons") or {}
    recovery_checked = int(recovery.get("input_candidates") or 0)
    recovery_429 = int(recovery_reasons.get("HTTP_429") or 0)
    recovery_timeouts = int(recovery_reasons.get("TIMEOUT") or 0)
    recovery_errors = int(recovery_reasons.get("NETWORK_ERROR") or 0) + sum(
        int(v or 0) for k, v in recovery_reasons.items() if str(k).startswith("HTTP_5")
    )
    canonical_rejected = int(pf_fast.get("canonical_domain_rejected") or 0) + int(pf_recovery.get("canonical_domain_rejected") or 0)

    summary = {
        "provider": a.provider,
        "cycle_id": a.cycle_id,
        "lane": a.lane,
        "verify_engine": a.verify_engine,
        "shard": {
            "name": a.name,
            "country": a.country,
            "region": a.region,
            "bbox": a.bbox,
            "release": a.release,
            "lane": a.lane,
        },
        "status": status,
        "error": err,
        "local_workers": a.local_workers,
        "contact_workers": a.contact_workers if a.lane == "site_recovery" else 0,
        "elapsed_seconds": round(time.time() - t0, 2),
        "raw_site_email_rows": int(fast.get("raw_site_email_rows") or fast.get("raw_site_rows") or 0),
        "recovery_candidates": int(fast.get("recovery_candidates") or 0),
        "canonical_prefilter_rejected": canonical_rejected,
        "recovered_public_emails": int(recovery.get("recovered_public_emails") or 0),
        "contact_ready": int(contact_ready.get("contact_ready") or 0),
        "social_or_contact_without_email": int(contact_ready.get("social_or_contact_without_email") or 0),
        "fast_ready": int(live.get("input_fast_ready") or fast.get("fast_ready") or 0),
        "live_high": int(live.get("live_high") or 0),
        "live_medium": int(live.get("live_medium") or 0),
        "live_ready": int(live.get("live_ready") or 0),
        "instagram_found": int(live.get("instagram_found") or recovery.get("instagram_found") or 0),
        "facebook_found": int(recovery.get("facebook_found") or 0),
        "contact_pages_found": int(recovery.get("contact_pages_found") or 0),
        "http_429_rate": round(rate429, 5),
        "timeout_rate": round(timeouts / checked if checked else 0, 5),
        "error_rate": round(errors / checked if checked else 0, 5),
        "contact_429_rate": round(recovery_429 / recovery_checked if recovery_checked else 0, 5),
        "contact_timeout_rate": round(recovery_timeouts / recovery_checked if recovery_checked else 0, 5),
        "contact_error_rate": round(recovery_errors / recovery_checked if recovery_checked else 0, 5),
        "live_reasons": reasons,
        "contact_reasons": recovery_reasons,
    }
    fr.write_json(out / "worker_summary.json", summary)
    print(json.dumps(summary, indent=2))
    if status != "success":
        raise SystemExit(2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", required=True)
    ap.add_argument("--cycle-id", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--country", default="")
    ap.add_argument("--region", default="")
    ap.add_argument("--bbox", required=True)
    ap.add_argument("--release", default="2026-06-17.0")
    ap.add_argument("--max-rows", type=int, default=250000)
    ap.add_argument("--lane", choices=("fast_email", "site_recovery", "fresh_search", "wikidata_hospitality"), default="fast_email")
    ap.add_argument("--canonical-domains", default="")
    ap.add_argument("--local-workers", type=int, default=64)
    ap.add_argument("--contact-workers", type=int, default=48)
    ap.add_argument("--contact-timeout", type=float, default=7.0)
    ap.add_argument("--contact-max-pages", type=int, default=3)
    ap.add_argument("--contact-max-bytes", type=int, default=900000)
    ap.add_argument("--verify-engine", choices=("thread", "async"), default="thread")
    ap.add_argument("--per-host", type=int, default=4)
    ap.add_argument("--outdir", required=True)
    worker(ap.parse_args())


if __name__ == "__main__":
    main()
