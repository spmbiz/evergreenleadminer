#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path


def latest_sample(limit: int) -> tuple[str, list[dict]]:
    files = sorted(Path("gpt/gws_review").glob("*.jsonl"), reverse=True)
    for path in files:
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("outcome") in {"REVIEW", "UNCERTAIN"} and not row.get("owned_website"):
                rows.append(row)
            if len(rows) >= limit:
                break
        if rows:
            return str(path), rows
    return "", []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", default="16,32,64")
    ap.add_argument("--sample", type=int, default=24)
    ap.add_argument("--out", default="results/gws_async_benchmark.json")
    args = ap.parse_args()
    values = [int(x) for x in args.concurrency.split(",") if x.strip()]
    source, rows = latest_sample(args.sample)
    payload = {"source_batch": source, "sample_records": len(rows), "tested_concurrency": values, "results": []}
    if not rows:
        payload["status"] = "no_sample"
    else:
        with tempfile.TemporaryDirectory(prefix="gws-async-bench-") as td:
            root = Path(td)
            for c in values:
                shard = root / f"c{c}"
                shard.mkdir(parents=True, exist_ok=True)
                (shard / "records.jsonl").write_text(
                    "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows), encoding="utf-8"
                )
                cp = subprocess.run(
                    [sys.executable, "tools/gws_async_probe.py", "--shard-dir", str(shard), "--concurrency", str(c),
                     "--per-host", "2", "--max-domains", "6", "--timeout", "4.5"],
                    text=True, capture_output=True,
                )
                metrics = {}
                mp = shard / "metrics.json"
                if mp.exists():
                    try:
                        metrics = json.loads(mp.read_text(encoding="utf-8")).get("async_probe", {})
                    except Exception:
                        metrics = {}
                payload["results"].append({
                    "concurrency": c,
                    "returncode": cp.returncode,
                    "elapsed_seconds": metrics.get("elapsed_seconds"),
                    "requests_per_second": metrics.get("requests_per_second"),
                    "requests": metrics.get("requests"),
                    "http_success": metrics.get("http_success"),
                    "owned_sites_found": metrics.get("owned_sites_found"),
                    "rate_429": metrics.get("rate_429"),
                    "errors": metrics.get("errors"),
                    "stdout_tail": cp.stdout[-1200:],
                    "stderr_tail": cp.stderr[-1200:],
                })
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
