#!/usr/bin/env python3
"""Wrap the existing master planner with expiring in-flight work exclusions.

The underlying yield ranking is unchanged. We simply request enough ranked
candidates to replace currently leased units, remove overlaps, and trim back to
the broker-allocated runner capacity.
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read_keys(path: str) -> set[str]:
    if not path:
        return set()
    try:
        return {x.strip() for x in Path(path).read_text(encoding="utf-8").splitlines() if x.strip()}
    except Exception:
        return set()


def task_keys(item: dict) -> list[str]:
    payload = str(item.get("batch_cells_b64") or "")
    if payload:
        try:
            cells = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
            keys = [str(x.get("key") or "") for x in cells if isinstance(x, dict) and x.get("key")]
            if keys:
                return keys
        except Exception:
            pass
    k = str(item.get("key") or "")
    return [k] if k else []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=("github", "circleci"), default="github")
    ap.add_argument("--capacity", type=int, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--exclude-keys", default="")
    ap.add_argument("--ignore-coverage", action="store_true")
    ap.add_argument("--force-lane", default="")
    a = ap.parse_args()

    excluded = read_keys(a.exclude_keys)
    capacity = max(0, int(a.capacity))
    # In the worst case every currently leased unit would otherwise occupy a top
    # rank. Ask for replacement depth without ever emitting the extra capacity.
    expanded = capacity + min(len(excluded), max(20, capacity * 3))
    if capacity == 0:
        expanded = 0

    tmp = Path(a.out).with_suffix(".unfiltered.json")
    cmd = [
        sys.executable, "tools/hospitality_master_plan.py",
        "--provider", a.provider,
        "--capacity", str(expanded),
        "--out", str(tmp),
    ]
    if a.ignore_coverage:
        cmd.append("--ignore-coverage")
    if a.force_lane:
        cmd += ["--force-lane", a.force_lane]
    subprocess.run(cmd, cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
    plan = json.loads(tmp.read_text(encoding="utf-8"))
    try:
        tmp.unlink()
    except Exception:
        pass

    kept = []
    excluded_tasks = 0
    excluded_units = 0
    for item in plan.get("include") or []:
        keys = task_keys(item)
        overlap = set(keys) & excluded
        if overlap:
            excluded_tasks += 1
            excluded_units += len(overlap)
            continue
        if len(kept) < capacity:
            kept.append(item)

    for i, item in enumerate(kept):
        item["slot"] = i
    lane_counts = {}
    for item in kept:
        lane = str(item.get("lane") or "unknown")
        lane_counts[lane] = lane_counts.get(lane, 0) + 1

    plan.update({
        "capacity": capacity,
        "planner_expanded_capacity": expanded,
        "inflight_keys_seen": len(excluded),
        "inflight_tasks_excluded": excluded_tasks,
        "inflight_work_units_excluded": excluded_units,
        "selected_lane_counts": lane_counts,
        "include": kept,
    })
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in plan.items() if k != "include"}, indent=2))


if __name__ == "__main__":
    main()
