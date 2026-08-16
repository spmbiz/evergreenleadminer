#!/usr/bin/env python3
"""Run the existing GWS planner against capacity already leased by the global broker.

The underlying planner normally re-measures account-wide GitHub activity. Once a
capacity lease has been atomically reserved, that second subtraction would count
hospitality/tender/GWS work twice. This thin wrapper disables only the redundant
GitHub activity probe; all GWS source-health, task, lease and matrix logic stays
inside gws_fleet_plan.py.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

import gws_fleet_plan as gp


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
    # gws_fleet_plan.py currently calls github_repo_job_counts(), not the old
    # github_active_jobs() helper. Stub the live probe so --fixed-capacity is
    # consumed as the already-leased budget instead of subtracting account-wide
    # jobs a second time. Keep the old symbol stub too for backward compatibility.
    gp.github_repo_job_counts = lambda *args, **kwargs: (0, 0, [])
    if hasattr(gp, "github_active_jobs"):
        gp.github_active_jobs = lambda *args, **kwargs: (0, [])

    sys.argv = [
        "gws_fleet_plan.py",
        "--provider", "github",
        "--fixed-capacity", str(a.capacity),
        "--outdir", str(outdir),
    ]
    gp.main()


if __name__ == "__main__":
    main()
