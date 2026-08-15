#!/usr/bin/env python3
"""Production wrapper for one approved AllThePlaces hospitality spider.

The source adapter is responsible for provenance semantics. This wrapper only
processes production-enabled spiders, prefilters known canonical domains before
HTTP, applies the standard current hospitality live gate, and emits the same
worker_summary contract consumed by the single canonical writer.
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
from pathlib import Path

import fleet_runtime as fr

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/atp_hospitality_spiders.json"


def policy(spider: str) -> dict:
    cfg = fr.load_json(CONFIG, {})
    for row in cfg.get("spiders") or []:
        if row.get("spider") == spider:
            return row
    raise RuntimeError(f"ATP spider not in policy: {spider}")


def run(cmd):
    p = subprocess.run(cmd, cwd=ROOT, text=True)
    if p.returncode:
        raise RuntimeError(f"exit {p.returncode}: {' '.join(cmd)}")


def live_reason_rates(path: Path):
    reasons = {}
    if path.exists():
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
    ap.add_argument("--spider", required=True)
    ap.add_argument("--expected-release", default="")
    ap.add_argument("--canonical-domains", default="")
    ap.add_argument("--local-workers", type=int, default=32)
    ap.add_argument("--outdir", required=True)
    a = ap.parse_args()

    out = Path(a.outdir)
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    status, error = "success", ""
    p = policy(a.spider)
    if not p.get("production_enabled"):
        raise SystemExit(f"ATP spider is not production-enabled: {a.spider}")
    mode = str(p.get("mode") or "")
    try:
        run([sys.executable, "tools/atp_spider_hospitality.py", "--spider", a.spider, "--outdir", str(out)])
        src = fr.load_json(out / "atp_spider_summary.json", {})
        actual_release = str(src.get("actual_run") or "")
        if a.expected_release and actual_release and a.expected_release != actual_release:
            # A source can roll between planner and worker. Process the fresher
            # successful output, but surface the race; coverage stores actual run.
            (out / "source_rollover.json").write_text(json.dumps({"planned":a.expected_release,"actual":actual_release}, indent=2) + "\n", encoding="utf-8")
        if mode not in ("trusted_directory_contact", "first_party"):
            raise RuntimeError(f"production mode not canonical-ready: {mode}")
        if a.canonical_domains and Path(a.canonical_domains).exists():
            run([sys.executable, "tools/filter_canonical_domains.py", "--input", str(out / "v6_fast_ready.csv"), "--domains", a.canonical_domains, "--stats", str(out / "canonical_prefilter.json")])
        run([sys.executable, "tools/v6_live_verify.py", "--input", str(out / "v6_fast_ready.csv"), "--outdir", str(out), "--workers", str(a.local_workers), "--timeout", "8"])
    except Exception as e:
        status = "failed_retryable"
        error = f"{type(e).__name__}: {e}"

    src = fr.load_json(out / "atp_spider_summary.json", {})
    live = fr.load_json(out / "v6_live_summary.json", {})
    pf = fr.load_json(out / "canonical_prefilter.json", {})
    actual_release = str(src.get("actual_run") or a.expected_release or "unknown")
    reasons, r429, timeout_rate, error_rate = live_reason_rates(out / "v6_live_verified.csv")
    summary = {
        "provider": a.provider,
        "cycle_id": a.cycle_id,
        "lane": "atp_directory_contact" if mode == "trusted_directory_contact" else "atp_first_party",
        "task_type": "atp_spider",
        "spider": a.spider,
        "shard": {
            "name": f"ATP::{a.spider}",
            "country": "GLOBAL",
            "region": f"ATP::{a.spider}",
            "bbox": f"atp:{a.spider}",
            "release": actual_release,
        },
        "status": status,
        "error": error,
        "local_workers": a.local_workers,
        "elapsed_seconds": round(time.time() - t0, 2),
        "raw_site_email_rows": int(src.get("features_seen") or 0),
        "canonical_prefilter_rejected": int(pf.get("canonical_domain_rejected") or 0),
        "fast_ready": int(live.get("input_fast_ready") or 0),
        "live_high": int(live.get("live_high") or 0),
        "live_medium": int(live.get("live_medium") or 0),
        "live_ready": int(live.get("live_ready") or 0),
        "instagram_found": int(live.get("instagram_found") or 0),
        "http_429_rate": r429,
        "timeout_rate": timeout_rate,
        "error_rate": error_rate,
        "live_reasons": reasons,
        "source_actual_output_url": src.get("actual_output_url") or "",
        "source_actual_run": actual_release,
    }
    fr.write_json(out / "worker_summary.json", summary)
    print(json.dumps(summary, indent=2))
    if status != "success":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
