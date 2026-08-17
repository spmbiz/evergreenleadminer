#!/usr/bin/env python3
"""Prepare bounded shadow-semantic work inside an already allocated GWS verifier.

This module never mutates strict verification. It only selects fresh MEDIUM /
UNCERTAIN rows emitted by the strict verifier, compacts them with the existing
semantic schema, and hands them to the existing Qwen semantic worker on the same
GitHub runner. The normal standalone semantic fleet remains available for
benchmarking and overflow/backfill.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from gws_semantic_plan import compact, load


def emit_output(name: str, value) -> None:
    gh = os.environ.get("GITHUB_OUTPUT")
    if gh:
        with open(gh, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shard-dir", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--config", default="config/gws_semantic_v1.json")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--worker-index", default="0")
    a = ap.parse_args()

    cfg = load(a.config, {})
    compound_cfg = cfg.get("compound_sidecar") or {}
    enabled = bool(compound_cfg.get("enabled", True)) and bool((cfg.get("qwen") or {}).get("enabled", True))
    configured_limit = max(0, int(compound_cfg.get("records_per_verify_worker") or 8))
    limit = max(0, int(a.limit)) if int(a.limit) > 0 else configured_limit

    out = Path(a.outdir)
    out.mkdir(parents=True, exist_ok=True)
    selected_path = out / "selected.jsonl"
    meta_path = out / "plan.json"

    source = Path(a.shard_dir) / "records.jsonl"
    rows = []
    if enabled and source.exists():
        for line in source.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue

    q = cfg.get("qwen") or {}
    model = str(q.get("model_label") or "qwen3-4b-q4_k_m")
    prompt = str(q.get("prompt_version") or "gws-semantic-v1")
    live_out = {str(x).upper() for x in (cfg.get("selection") or {}).get("live_outcomes") or []}
    live_st = {str(x).upper() for x in (cfg.get("selection") or {}).get("live_statuses") or []}
    prior = load("state/gws_semantic_index.json", {"records": {}}).get("records", {})

    selected = []
    eligible = 0
    already_done = 0
    for row in rows:
        outcome = str(row.get("outcome") or "").upper()
        status = str(row.get("verification_status") or "").upper()
        if outcome not in live_out and status not in live_st:
            continue
        eligible += 1
        rec = compact(row, model, prompt)
        bid = str(rec.get("business_id") or "")
        old = prior.get(bid) or {}
        if (
            bid
            and old.get("semantic_fingerprint") == rec.get("semantic_fingerprint")
            and old.get("model") == model
            and old.get("prompt_version") == prompt
        ):
            already_done += 1
            continue
        selected.append(rec)
        if limit and len(selected) >= limit:
            break

    with selected_path.open("w", encoding="utf-8") as f:
        for row in selected:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True, default=str) + "\n")

    plan = {
        "schema_version": 1,
        "mode": "compound_same_gws_worker_shadow",
        "worker_index": str(a.worker_index),
        "enabled": enabled,
        "source_records": len(rows),
        "eligible_ambiguous": eligible,
        "already_classified_same_fingerprint": already_done,
        "selected": len(selected),
        "limit": limit,
        "model": model,
        "prompt_version": prompt,
        "strict_mutations_allowed": False,
        "standalone_semantic_role": "benchmark_and_overflow_backfill",
    }
    meta_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    emit_output("selected", len(selected))
    emit_output("input", str(selected_path))
    print("GWS_COMPOUND_SEMANTIC=" + json.dumps(plan, separators=(",", ":")))


if __name__ == "__main__":
    main()
