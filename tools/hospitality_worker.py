#!/usr/bin/env python3
"""Provider-neutral hospitality worker with safe negative-bbox CLI handling.

Supports the legacy thread/requests verifier and an experimental bounded
asyncio/aiohttp verifier. Missing or blocked sites are withheld rather than
inferred.
"""
from __future__ import annotations
import argparse, csv, json, sys, time
from pathlib import Path
import fleet_runtime as fr


def worker(a):
    out = Path(a.outdir)
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    status = "success"
    err = ""
    try:
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
        verifier = "tools/v6_live_verify_async.py" if a.verify_engine == "async" else "tools/v6_live_verify.py"
        cmd = [
            sys.executable, verifier,
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
    live = fr.load_json(out / "v6_live_summary.json", {})
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
    summary = {
        "provider": a.provider, "cycle_id": a.cycle_id, "verify_engine": a.verify_engine,
        "shard": {"name": a.name, "country": a.country, "region": a.region, "bbox": a.bbox, "release": a.release},
        "status": status, "error": err, "local_workers": a.local_workers,
        "elapsed_seconds": round(time.time() - t0, 2),
        "raw_site_email_rows": int(fast.get("raw_site_email_rows") or 0),
        "fast_ready": int(fast.get("fast_ready") or 0),
        "live_high": int(live.get("live_high") or 0),
        "live_medium": int(live.get("live_medium") or 0),
        "live_ready": int(live.get("live_ready") or 0),
        "instagram_found": int(live.get("instagram_found") or 0),
        "http_429_rate": round(rate429, 5),
        "timeout_rate": round(timeouts / checked if checked else 0, 5),
        "error_rate": round(errors / checked if checked else 0, 5),
        "live_reasons": reasons,
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
    ap.add_argument("--local-workers", type=int, default=64)
    ap.add_argument("--verify-engine", choices=("thread", "async"), default="thread")
    ap.add_argument("--per-host", type=int, default=4)
    ap.add_argument("--outdir", required=True)
    worker(ap.parse_args())

if __name__ == "__main__":
    main()
