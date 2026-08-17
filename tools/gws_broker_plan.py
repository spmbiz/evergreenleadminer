#!/usr/bin/env python3
"""Run the existing GWS planner against capacity already leased by the global broker.

The underlying planner normally re-measures account-wide GitHub activity. Once a
capacity lease has been atomically reserved, that second subtraction would count
hospitality/tender/GWS work twice. This thin wrapper disables only the redundant
GitHub activity probe; all GWS source-health, task, lease and matrix logic stays
inside gws_fleet_plan.py.

When a validated Overture supply matrix matches the current durable source
release+SHA *and* the current category-mapper semantics, this wrapper also avoids
proven source-empty territory×family shards and gives a bounded priority bonus to
high-supply shards. This is discovery routing only: it never changes website-state
classification or strict HIGH rules.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

import gws_fleet_plan as gp

# Bump whenever category->GWS-family materialization semantics change. A supply
# matrix produced by an older mapper is discovery-routing evidence from a different
# universe and must fail closed instead of steering current work.
CURRENT_SUPPLY_MAPPER_VERSION = "overture-category-token-safe-v2"


def emit_zero(outdir: Path):
    outdir.mkdir(parents=True, exist_ok=True)
    plan = {
        "schema_version": 1,
        "provider": "github",
        "enabled": True,
        "selected_count": 0,
        "max_parallel": 0,
        "matrix": {"include": []},
        "tasks": [],
        "broker_capacity": 0,
    }
    (outdir / "plan.json").write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    gh = os.environ.get("GITHUB_OUTPUT")
    if gh:
        with open(gh, "a", encoding="utf-8") as f:
            f.write("enabled=true\nselected_count=0\nmax_parallel=0\nmatrix={\"include\":[]}\n")
    print(json.dumps(plan, indent=2))


def _load(path: str, default):
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def install_supply_routing() -> dict:
    """Monkey-patch only planner task ordering when matrix+source+mapper are current."""
    supply = _load("state/gws_supply_matrix.json", {})
    source_state = _load("state/gws_source_state.json", {})
    overture = source_state.get("sources", {}).get("overture_direct", {})
    release_ok = bool(supply.get("source_release")) and supply.get("source_release") == overture.get("release")
    sha_ok = bool(supply.get("source_sha256")) and supply.get("source_sha256") == overture.get("sha256")
    mapper_ok = supply.get("mapper_version") == CURRENT_SUPPLY_MAPPER_VERSION
    active = bool(release_ok and sha_ok and mapper_ok)
    status = {
        "active": active,
        "release_match": release_ok,
        "sha_match": sha_ok,
        "mapper_match": mapper_ok,
        "current_mapper_version": CURRENT_SUPPLY_MAPPER_VERSION,
        "matrix_mapper_version": supply.get("mapper_version"),
        "source_release": overture.get("release"),
        "matrix_release": supply.get("source_release"),
        "zero_tasks": int(supply.get("zero_task_count") or 0),
        "boosted_tasks": len(supply.get("top_supply_tasks", {})),
    }
    if not active:
        print("GWS_SUPPLY_ROUTING=" + json.dumps(status, separators=(",", ":")))
        return status

    zero = set(str(x) for x in supply.get("zero_tasks", []))
    top = {str(k): int(v) for k, v in supply.get("top_supply_tasks", {}).items() if int(v) > 0}
    original = gp.build_tasks

    def supply_aware_build_tasks(workloads: dict, coverage: dict) -> list[dict]:
        tasks = original(workloads, coverage)
        routed: list[dict] = []
        skipped = 0
        boosted = 0
        for task in tasks:
            key = f"{task.get('territory')}::{task.get('family')}"
            # Matrix is South-only. Never infer zero outside explicitly measured keys.
            if key in zero:
                skipped += 1
                continue
            estimate = top.get(key)
            if estimate:
                # Bounded secondary signal. Never/retry/staleness semantics from the
                # core planner remain dominant; this only orders comparable fresh work.
                task["supply_estimate"] = estimate
                task["priority_score"] = round(float(task.get("priority_score") or 0) + min(5000.0, estimate * 20.0), 3)
                boosted += 1
            routed.append(task)
        routed.sort(key=lambda x: (-float(x.get("priority_score") or 0), x["task_id"]))
        print("GWS_SUPPLY_TASK_ROUTING=" + json.dumps({
            "input_tasks": len(tasks),
            "output_tasks": len(routed),
            "skipped_validated_zero": skipped,
            "boosted_high_supply": boosted,
        }, separators=(",", ":")))
        return routed

    gp.build_tasks = supply_aware_build_tasks
    print("GWS_SUPPLY_ROUTING=" + json.dumps(status, separators=(",", ":")))
    return status


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--capacity", type=int, required=True)
    ap.add_argument("--outdir", required=True)
    a = ap.parse_args()
    outdir = Path(a.outdir)
    if a.capacity <= 0:
        emit_zero(outdir)
        return

    # Capacity is already atomically reserved by global_capacity_broker.py.
    gp.github_repo_job_counts = lambda *args, **kwargs: (0, 0, [])
    if hasattr(gp, "github_active_jobs"):
        gp.github_active_jobs = lambda *args, **kwargs: (0, [])

    install_supply_routing()

    sys.argv = [
        "gws_fleet_plan.py",
        "--provider", "github",
        "--fixed-capacity", str(a.capacity),
        "--outdir", str(outdir),
    ]
    gp.main()


if __name__ == "__main__":
    main()
