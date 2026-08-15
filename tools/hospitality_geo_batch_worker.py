#!/usr/bin/env python3
"""Run multiple independent hospitality geo work units on one GitHub runner.

This is runner-local work stealing / setup amortization. Each child unit uses the
existing hospitality_worker implementation and writes to its own subdirectory,
so the existing aggregate still sees immutable per-shard summaries and
v6_live_ready partitions exactly as before.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import time
from pathlib import Path
from types import SimpleNamespace

import hospitality_worker as hw


def safe_part(value: str) -> str:
    v = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "")).strip("-.")
    return v[:80] or "cell"


def decode_cells(payload: str) -> list[dict]:
    raw = base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8")
    doc = json.loads(raw)
    if not isinstance(doc, list):
        raise ValueError("batch payload must be a list")
    return [x for x in doc if isinstance(x, dict) and x.get("bbox")]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", required=True)
    ap.add_argument("--cycle-id", required=True)
    ap.add_argument("--cells-b64", required=True)
    ap.add_argument("--canonical-domains", default="")
    ap.add_argument("--outdir", required=True)
    a = ap.parse_args()

    cells = decode_cells(a.cells_b64)
    root = Path(a.outdir)
    root.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    outcomes = []

    for i, cell in enumerate(cells, 1):
        child = root / f"{i:02d}-{safe_part(cell.get('key') or cell.get('name') or 'cell')}"
        ns = SimpleNamespace(
            provider=a.provider,
            cycle_id=a.cycle_id,
            name=str(cell.get("name") or f"batch-cell-{i}"),
            country=str(cell.get("country") or ""),
            region=str(cell.get("region") or ""),
            bbox=str(cell.get("bbox") or ""),
            release=str(cell.get("release") or "2026-06-17.0"),
            max_rows=int(cell.get("max_rows") or 250000),
            lane=str(cell.get("lane") or "fast_email"),
            canonical_domains=a.canonical_domains,
            local_workers=int(cell.get("local_workers") or 32),
            contact_workers=int(cell.get("contact_workers") or 48),
            contact_timeout=float(cell.get("contact_timeout") or 7.0),
            contact_max_pages=int(cell.get("contact_max_pages") or 3),
            contact_max_bytes=int(cell.get("contact_max_bytes") or 900000),
            verify_engine=str(cell.get("verify_engine") or "thread"),
            per_host=int(cell.get("per_host") or 4),
            outdir=str(child),
        )
        code = 0
        error = ""
        started = time.time()
        try:
            hw.worker(ns)
        except SystemExit as exc:
            code = int(exc.code or 1)
            error = f"SystemExit:{code}"
        except Exception as exc:
            code = 2
            error = f"{type(exc).__name__}: {exc}"
        outcomes.append({
            "key": cell.get("key"),
            "name": ns.name,
            "lane": ns.lane,
            "exit_code": code,
            "error": error,
            "elapsed_seconds": round(time.time() - started, 2),
            "outdir": str(child),
        })

    success = sum(x["exit_code"] == 0 for x in outcomes)
    summary = {
        "cycle_id": a.cycle_id,
        "provider": a.provider,
        "cells": len(cells),
        "cells_succeeded": success,
        "cells_failed": len(cells) - success,
        "elapsed_seconds": round(time.time() - t0, 2),
        "outcomes": outcomes,
    }
    (root / "batch_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    if cells and success == 0:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
