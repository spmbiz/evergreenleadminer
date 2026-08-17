#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import global_capacity_broker_v3 as v3

ROOT = Path(__file__).resolve().parents[1]
HARD_TERMINAL = {"HIGH", "REJECT", "DUPLICATE", "ERROR_HARD"}
SOFT = {"MEDIUM", "UNCERTAIN"}


def _load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _strict_backlog() -> int:
    """Mirror the strict planner's eligibility gate without mutating state.

    Fail closed in favour of the strict verifier: if the signal cannot be
    computed, return one so standalone semantic capacity yields to compound
    same-runner Qwen rather than stealing strict GWS slots.
    """
    try:
        pending = _load(ROOT / "gpt/gws_pending_batches.json", {"batches": []})
        verify_index = _load(ROOT / "state/gws_verify_index.json", {"records": {}}).get("records", {})
        semantic_index = _load(ROOT / "state/gws_semantic_index.json", {"records": {}}).get("records", {})
        latest = {}
        for batch in pending.get("batches") or []:
            if batch.get("status") != "pending" or not batch.get("batch"):
                continue
            p = ROOT / str(batch["batch"])
            if not p.exists():
                continue
            for line in p.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                key = str(row.get("record_key") or "")
                if key:
                    latest[key] = row

        eligible = 0
        for key, row in latest.items():
            prior = verify_index.get(key) or {}
            fp = str(row.get("fingerprint") or "")
            prior_status = str(prior.get("verification_status") or "").strip().upper()
            same = prior.get("source_fingerprint") == fp
            if same and prior_status in HARD_TERMINAL:
                continue

            semantic_recheck = False
            if same and prior_status in SOFT:
                sem = semantic_index.get(key) or {}
                sem_fp = str(sem.get("semantic_fingerprint") or "")
                sem_status = str(sem.get("resolution_status") or "").upper()
                prior_sem_fp = str(prior.get("semantic_resolution_fingerprint") or "")
                prior_attempt = int(prior.get("semantic_resolution_attempt") or 0)
                if sem_status == "QUEUED" and sem_fp and sem_fp != prior_sem_fp and prior_attempt < 2:
                    semantic_recheck = True
                else:
                    continue

            if row.get("outcome") == "REJECT" and not semantic_recheck:
                continue
            eligible += 1
        return eligible
    except Exception:
        return 1


def _policy() -> tuple[int, int, int]:
    semantic_cfg = _load(ROOT / os.environ.get("SEMANTIC_CONFIG", "config/gws_semantic_v1.json"), {})
    rollout = semantic_cfg.get("rollout") or {}
    fleet_cfg = _load(ROOT / "config/global_fleet.json", {})
    gws_cfg = ((fleet_cfg.get("workloads") or {}).get("gws") or {})
    gws_floor = max(0, int(gws_cfg.get("min_slots_when_demanding") or 0))
    strict_reserved = max(0, int(rollout.get("strict_reserved_slots") or gws_floor))
    configured_semantic_cap = max(0, int(rollout.get("semantic_max_workers_when_strict_demand") or 0))
    semantic_cap = min(configured_semantic_cap, max(0, gws_floor - strict_reserved))
    return strict_reserved, semantic_cap, gws_floor


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demand", type=int, required=True)
    ap.add_argument("--requested", type=int, required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--owner", default="spmbiz")
    ap.add_argument("--repo", default="spmbiz/evergreenleadminer")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    a.dry_run = False
    a.workload = "gws"

    strict_backlog = _strict_backlog()
    strict_reserved, semantic_cap, gws_floor = _policy()
    requested_before = max(0, int(a.requested))
    if strict_backlog > 0:
        a.requested = min(requested_before, semantic_cap)

    original = v3.useful_gws_count
    v3.useful_gws_count = lambda: max(int(a.demand), int(original()))
    try:
        v3.reserve(a)
    finally:
        v3.useful_gws_count = original

    out = Path(a.out)
    decision = _load(out, {})
    if isinstance(decision, dict):
        decision.update({
            "gws_lane": "semantic_overflow",
            "strict_backlog": strict_backlog,
            "strict_reserved_slots": strict_reserved,
            "gws_floor_slots": gws_floor,
            "semantic_requested_before_strict_reserve": requested_before,
            "semantic_request_after_strict_reserve": max(0, int(a.requested)),
            "semantic_cap_when_strict_demand": semantic_cap,
            "reservation_policy": "compound-qwen-inside-strict-workers; standalone-semantic-zero-while-strict-demand; elastic-overflow-when-strict-empty",
        })
        out.write_text(json.dumps(decision, indent=2) + "\n", encoding="utf-8")
        print("GWS_SEMANTIC_STRICT_RESERVE=" + json.dumps({
            "strict_backlog": strict_backlog,
            "strict_reserved_slots": strict_reserved,
            "gws_floor_slots": gws_floor,
            "requested_before": requested_before,
            "requested_after": max(0, int(a.requested)),
            "allocated": int(decision.get("allocated") or 0),
        }, separators=(",", ":")))


if __name__ == "__main__":
    main()
