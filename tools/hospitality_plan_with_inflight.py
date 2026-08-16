#!/usr/bin/env python3
"""Wrap the existing master planner with expiring in-flight work exclusions.

The underlying yield ranking is unchanged. We request enough ranked candidates
to replace leased geographic units, remove overlaps, and trim to broker capacity.
For capped external sources, replacement happens inside the source cap too: an
in-flight fresh-search shard must not consume one of the fresh slots and silently
turn it into geo work. A bounded Wikidata overlay may then replace one selected
unit with an independent structured-source exploration task.

A deliberately bounded stress-test control may temporarily force one existing
lane. Normal production ignores the control override unless the mode explicitly
starts with ``five-minute-live-max-throughput``.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

import fleet_runtime as fr
import hospitality_master_plan as hm
import hospitality_wikidata_plan as hw

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


def bounded_control_lane(cli_lane: str) -> str:
    if cli_lane:
        return cli_lane
    try:
        control = json.loads((ROOT / "control/hospitality_test.json").read_text(encoding="utf-8"))
    except Exception:
        return ""
    mode = str(control.get("mode") or "")
    lane = str(control.get("force_lane") or "").strip()
    if not mode.startswith("five-minute-live-max-throughput"):
        return ""
    allowed = {"fast_email", "site_recovery", "atp", "osm"}
    return lane if lane in allowed else ""


def fresh_replacements(excluded: set[str], kept: list[dict], capacity: int) -> list[dict]:
    """Return due fresh shards that can replace fresh shards excluded in-flight."""
    if capacity <= 0:
        return []
    try:
        coverage = (fr.load_json(ROOT / "state/coverage.json", {}).get("shards") or {})
        due, _errors, fresh_cap = hm.fresh_search_tasks(dt.datetime.now(dt.timezone.utc), coverage)
    except Exception:
        return []
    current = sum(str(x.get("lane") or "") == "fresh_search" for x in kept)
    need = max(0, min(int(fresh_cap or 0), capacity) - current)
    if need <= 0:
        return []
    occupied = {k for item in kept for k in task_keys(item)}
    out = []
    for item in due:
        keys = task_keys(item)
        if not keys or (set(keys) & excluded) or (set(keys) & occupied):
            continue
        out.append(item)
        occupied.update(keys)
        if len(out) >= need:
            break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=("github", "circleci"), default="github")
    ap.add_argument("--capacity", type=int, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--exclude-keys", default="")
    ap.add_argument("--ignore-coverage", action="store_true")
    ap.add_argument("--force-lane", default="")
    a = ap.parse_args()

    force_lane = bounded_control_lane(a.force_lane)
    excluded = read_keys(a.exclude_keys)
    capacity = max(0, int(a.capacity))
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
    if force_lane:
        cmd += ["--force-lane", force_lane]
    subprocess.run(cmd, cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
    plan = json.loads(tmp.read_text(encoding="utf-8"))
    try:
        tmp.unlink()
    except Exception:
        pass

    kept = []
    excluded_tasks = 0
    excluded_units = 0
    excluded_fresh = 0
    for item in plan.get("include") or []:
        keys = task_keys(item)
        overlap = set(keys) & excluded
        if overlap:
            excluded_tasks += 1
            excluded_units += len(overlap)
            if str(item.get("lane") or "") == "fresh_search":
                excluded_fresh += 1
            continue
        if len(kept) < capacity:
            kept.append(item)

    replacements = []
    if excluded_fresh:
        replacements = fresh_replacements(excluded, kept, capacity)
        if replacements:
            kept = (replacements + kept)[:capacity]

    # Independent structured discovery is injected only after normal master-plan
    # selection/in-flight filtering. It stays bounded to its own source cap and
    # does not increase the broker allocation or total runner count.
    plan["include"] = kept
    if not force_lane:
        plan = hw.inject(plan, excluded)
    kept = list(plan.get("include") or [])[:capacity]

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
        "inflight_fresh_tasks_excluded": excluded_fresh,
        "fresh_inflight_replacements": len(replacements),
        "stress_force_lane": force_lane,
        "selected_lane_counts": lane_counts,
        "include": kept,
    })
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in plan.items() if k != "include"}, indent=2))


if __name__ == "__main__":
    main()
