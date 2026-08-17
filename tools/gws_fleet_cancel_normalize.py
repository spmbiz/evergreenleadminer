#!/usr/bin/env python3
"""Normalize intentionally cancelled/superseded GWS matrix gaps out of source-health autoscaling.

Useful completed shard output is still persisted. The only thing this guard changes is
how missing matrix shards are interpreted for health/autoscale when GitHub reports the
harvest dependency as cancelled. Executed worker failures remain real failures.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def load(path: str | Path, default):
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def dump(path: str | Path, value):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def worker_metrics(root: Path) -> list[dict]:
    out = []
    for p in root.rglob("metrics.json"):
        if "fleet_plan" in p.parts:
            continue
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan-dir", default="results/fleet_plan")
    ap.add_argument("--shards-root", default="results/shards")
    ap.add_argument("--harvest-result", default=os.environ.get("HARVEST_NEEDS_RESULT", ""))
    args = ap.parse_args()

    result = str(args.harvest_result or "").strip().lower()
    if result != "cancelled":
        print("GWS_CANCEL_NORMALIZE=" + json.dumps({"applied": False, "harvest_result": result or "unknown"}, separators=(",", ":")))
        return 0

    plan = load(Path(args.plan_dir) / "plan.json", {})
    selected = int(plan.get("selected_count") or 0)
    rows = worker_metrics(Path(args.shards_root))
    received = len(rows)
    missing = max(0, selected - received)
    executed_failed = sum(str(r.get("status") or "").lower() != "completed" for r in rows)
    executed_success = received - executed_failed
    executed_error_rate = executed_failed / max(1, received)

    metrics = load("metrics/gws_latest.json", {})
    summary = load("gpt/gws_latest_summary.json", {})
    profiles = load("state/gws_source_profiles.json", {"schema_version": 1, "profiles": {}})
    profile_name = str(plan.get("source_profile") or "gws_hub_overture")
    profile = profiles.setdefault("profiles", {}).setdefault(profile_name, {})

    before = int(metrics.get("source_parallel_before") or profile.get("stable_parallel") or 20)
    cfg = load("config/global_fleet.json", {})
    gws_cfg = (cfg.get("workloads") or {}).get("gws") or {}
    dedicated_cap = int(gws_cfg.get("max_slots") or 20)
    corrected_parallel = min(dedicated_cap, before)

    # If the only reason for backoff was missing shards from a cancelled run, restore
    # the prior healthy setting. Real executed failures still control backoff.
    if executed_error_rate >= 0.20:
        step = int(profile.get("scale_step") or 4)
        corrected_parallel = max(2, min(dedicated_cap, before - step))
        reason = "backoff_executed_worker_errors"
    else:
        reason = "hold_cancelled_missing_excluded"

    profile.update({
        "stable_parallel": corrected_parallel,
        "max_parallel": dedicated_cap,
        "last_error_rate": round(executed_error_rate, 4),
        "last_autoscale_reason": reason,
    })

    metrics.update({
        "worker_results_received": received,
        "successful_workers": executed_success,
        "executed_failed_workers": executed_failed,
        "cancelled_missing_workers": missing,
        "failed_or_missing_workers": executed_failed,
        "error_rate": round(executed_error_rate, 4),
        "source_parallel_after": corrected_parallel,
        "autoscale_reason": reason,
        "harvest_dependency_result": result,
    })

    summary["status"] = "CYCLE_PARTIAL" if (missing or executed_failed) else "CYCLE_COMPLETE"
    summary["errors"] = executed_failed
    summary["cancelled_missing_workers"] = missing
    summary["coverage_delta"] = {
        "tasks_completed": executed_success,
        "tasks_failed_or_missing": executed_failed,
        "tasks_cancelled_missing": missing,
    }
    summary["autoscaling"] = {"before": before, "after": corrected_parallel, "reason": reason}

    dump("metrics/gws_latest.json", metrics)
    dump("gpt/gws_latest_summary.json", summary)
    dump("state/gws_source_profiles.json", profiles)

    print("GWS_CANCEL_NORMALIZE=" + json.dumps({
        "applied": True,
        "selected": selected,
        "received": received,
        "executed_success": executed_success,
        "executed_failed": executed_failed,
        "cancelled_missing": missing,
        "effective_error_rate": round(executed_error_rate, 4),
        "parallel_before": before,
        "parallel_after": corrected_parallel,
        "reason": reason,
    }, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
