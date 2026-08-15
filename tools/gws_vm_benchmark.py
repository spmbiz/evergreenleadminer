#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from gws_source_overture import query_places


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threads", default="4,8,12,16")
    ap.add_argument("--out", default="results/gws_vm_benchmark.json")
    args = ap.parse_args()
    thread_values = [max(1, int(x)) for x in args.threads.split(",") if x.strip()]
    results = []
    for t in thread_values:
        started = time.perf_counter()
        try:
            rows, stats = query_places(t)
            elapsed = time.perf_counter() - started
            result = {
                "threads": t,
                "status": "ok",
                "elapsed_seconds": round(elapsed, 3),
                "places": len(rows),
                "places_per_second": round(len(rows) / max(elapsed, 0.001), 3),
                "source_stats": stats,
            }
        except Exception as exc:
            elapsed = time.perf_counter() - started
            result = {"threads": t, "status": "error", "elapsed_seconds": round(elapsed, 3), "error": f"{type(exc).__name__}: {exc}"}
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    ok = [r for r in results if r["status"] == "ok"]
    best = min(ok, key=lambda r: r["elapsed_seconds"]) if ok else None
    payload = {
        "os_cpu_count": os.cpu_count(),
        "tested_threads": thread_values,
        "best_threads": best.get("threads") if best else None,
        "best_elapsed_seconds": best.get("elapsed_seconds") if best else None,
        "results": results,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
