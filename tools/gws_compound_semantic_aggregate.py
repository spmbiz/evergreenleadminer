#!/usr/bin/env python3
"""Persist same-runner GWS semantic sidecars without touching strict truth/rollout.

The standalone semantic fleet owns benchmark/rollout progression. This compound
aggregator only deduplicates and persists fresh shadow semantic results produced
inside strict GWS verifier workers, so those results are immediately reusable and
are skipped by later overflow/backfill passes.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from collections import Counter
from pathlib import Path

from gws_semantic_aggregate import (
    append,
    dump,
    load,
    resolution_route,
    selected_candidate_host_class,
    selected_candidate_url,
)


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def iter_sidecar_records(root: str):
    for p in sorted(Path(root).rglob("compound_semantic/worker/records.jsonl")):
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    yield json.loads(line)
                except Exception:
                    continue


def iter_sidecar_summaries(root: str):
    for p in sorted(Path(root).rglob("compound_semantic/worker/summary.json")):
        try:
            yield json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--config", default="config/gws_semantic_v1.json")
    a = ap.parse_args()

    cfg = load(a.config, {})
    rows = list(iter_sidecar_records(a.root))
    summaries = list(iter_sidecar_summaries(a.root))
    ts = now()
    date = ts[:10]

    index = load("state/gws_semantic_index.json", {"schema_version": 1, "records": {}})
    records = index.setdefault("records", {})
    live_out = set((cfg.get("selection") or {}).get("live_outcomes") or [])
    live_st = set((cfg.get("selection") or {}).get("live_statuses") or [])

    fresh_rows = []
    review = []
    decisions = Counter()
    routes = Counter()
    errors = 0
    for row in rows:
        bid = str(row.get("business_id") or "")
        if not bid:
            continue
        sem = row.get("semantic") or {}
        old = records.get(bid) or {}
        if (
            old.get("semantic_fingerprint") == row.get("semantic_fingerprint")
            and old.get("model") == row.get("model")
            and old.get("prompt_version") == row.get("prompt_version")
        ):
            continue

        err = str(sem.get("_classifier_error") or "")
        errors += int(bool(err))
        decisions[str(sem.get("decision") or "UNCERTAIN")] += 1
        is_live = row.get("source_outcome") in live_out or row.get("source_verification_status") in live_st
        route = resolution_route(sem, row) if is_live else ""
        if route:
            routes[route] += 1
        selected = selected_candidate_url(sem, row)
        target = row.get("targeted_search") or {}
        records[bid] = {
            "semantic_fingerprint": row.get("semantic_fingerprint"),
            "source_fingerprint": row.get("source_fingerprint"),
            "certificate_digest": row.get("certificate_digest"),
            "model": row.get("model"),
            "prompt_version": row.get("prompt_version"),
            "decision": sem.get("decision"),
            "confidence": sem.get("confidence"),
            "website_state": sem.get("website_state"),
            "needs_gpt_review": bool(is_live or sem.get("needs_gpt_review")),
            "last_classified": ts,
            "classifier_error": err,
            "source_outcome": row.get("source_outcome"),
            "source_reason": row.get("source_reason"),
            "candidate_url": selected,
            "candidate_host_class": selected_candidate_host_class(sem, row),
            "candidate_set_size": len(row.get("candidate_set") or []),
            "targeted_search_first_party_confirmed": len(target.get("first_party_confirmed") or []),
            "resolution_status": "QUEUED" if is_live else "SHADOW_ONLY",
            "resolution_route": route,
            "compound_same_runner": True,
        }
        fresh_rows.append(row)
        if is_live:
            handoff = dict(row)
            handoff["semantic_selected_candidate_url"] = selected
            handoff["semantic_selected_candidate_host_class"] = selected_candidate_host_class(sem, row)
            handoff["resolution_route"] = route
            handoff["resolution_status"] = "QUEUED"
            handoff["semantic_queued_at"] = ts
            handoff["compound_same_runner"] = True
            review.append(handoff)

    if fresh_rows:
        append(Path("data/gws/semantic") / f"{date}.jsonl", fresh_rows)
    if review:
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
        append(Path("gpt/gws_semantic_review") / f"{stamp}-compound.jsonl", review)
    dump("state/gws_semantic_index.json", index)

    targeted_records = sum(int(x.get("targeted_search_records") or 0) for x in summaries)
    metrics = {
        "schema_version": 1,
        "at": ts,
        "mode": "compound_same_gws_worker_shadow",
        "worker_summaries": len(summaries),
        "worker_records_seen": len(rows),
        "fresh_semantic_records": len(fresh_rows),
        "deduped_existing": max(0, len(rows) - len(fresh_rows)),
        "classifier_errors": errors,
        "decisions": dict(decisions),
        "resolution_queued": len(review),
        "resolution_routes": dict(routes),
        "targeted_search_records": targeted_records,
        "canonical_high_mutations": 0,
        "strict_high_overrides": 0,
        "deterministic_reject_overrides": 0,
        "rollout_state_mutated": False,
    }
    dump("metrics/gws_compound_semantic_latest.json", metrics)
    append("metrics/gws_compound_semantic_history.jsonl", [metrics])
    print("GWS_COMPOUND_SEMANTIC_AGG=" + json.dumps(metrics, separators=(",", ":")))


if __name__ == "__main__":
    main()
