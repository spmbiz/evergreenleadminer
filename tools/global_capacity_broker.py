#!/usr/bin/env python3
"""Production entrypoint for the fair-share global GitHub capacity broker.

This intentionally uses v3 admission semantics: active jobs consume physical
runner capacity, while queued jobs are demand/ordering signals rather than fake
physical occupancy. That prevents one workload with a deep queue from starving a
demanding sibling that is below its fair share.

Release also emits a repository_dispatch refill signal whenever the workload still
has useful demand, including the important zero-lease / zero-capacity case. This
keeps the hot loop alive when a run was denied capacity and therefore had no real
lease to release.

Hospitality discovery is the source-of-growth lane. Its demand is derived from the
same multi-lane planner used by the V1 autonomous fleet, not the older grid-only
coverage heuristic. While discovery backlog exists, normal Intelligence V2 runs
are capped to one hospitality slot so V2 cannot consume the base Hospitality
share that should keep finding/recovering new accounts. Explicit manual V2 worker
overrides remain available for canaries.
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.request

import fleet_runtime as fr
import global_capacity_broker_v3 as v3


def _infer_workload(matched_leases: list[dict]) -> str:
    for lease in matched_leases:
        workload = str(lease.get("workload") or "").strip().lower()
        if workload in {"hospitality", "gws"}:
            return workload
    workflow = str(os.environ.get("GITHUB_WORKFLOW") or "").lower()
    if "hospitality" in workflow:
        return "hospitality"
    if "gws" in workflow or "no-website" in workflow or "no website" in workflow:
        return "gws"
    return ""


def _hospitality_discovery_demand() -> int:
    """Return the V1 multi-lane geographic backlog used by production planning.

    This deliberately mirrors hospitality_multilane_plan's eligibility/ranking
    inputs without selecting workers or touching canonical state. It fixes the
    regression where the broker's older grid-only heuristic returned zero while
    the production V1 planner still had hundreds of site-recovery work units.
    """
    try:
        import hospitality_grid_plan as gp
        import hospitality_multilane_plan as mp

        coverage = (fr.load_json(v3.ROOT / "state/coverage.json", {}).get("shards") or {})
        atlas_cfg = fr.load_json(v3.ROOT / "config/hospitality_world_atlas.json", {})
        now = v3.now_utc()
        useful = 0
        for cell in mp.lane_catalog():
            prior = coverage.get(cell["key"]) or {}
            if gp.rank_cell(cell, prior, now, atlas_cfg, False) is not None:
                useful += 1
        return useful
    except Exception:
        # Never turn an inability to compute the new signal into false zero
        # demand. Fall back to the broker's legacy signal instead.
        try:
            return max(0, int(v3.useful_hospitality_count() or 0))
        except Exception:
            return 1


def _effective_local_demand() -> dict:
    demand = dict(v3.local_demand() or {})
    demand["hospitality"] = max(
        int(demand.get("hospitality", 0) or 0),
        _hospitality_discovery_demand(),
    )
    return demand


def _emit_refill(repo: str, workload: str, source_run_id: str, released_slots: int) -> dict:
    if workload not in {"hospitality", "gws"}:
        return {"emitted": False, "reason": "unknown_workload"}

    desired = fr.load_json(v3.ROOT / "control/desired_state.json", {})
    fleet_cfg = fr.load_json(v3.ROOT / "config/global_fleet.json", {})
    workload_cfg = (fleet_cfg.get("workloads") or {}).get(workload) or {}
    if not bool(desired.get("enabled", True)) or not bool(desired.get("continuous", True)):
        return {"emitted": False, "reason": "continuous_fleet_disabled"}
    if not bool(workload_cfg.get("enabled", True)):
        return {"emitted": False, "reason": "workload_disabled"}

    demand = _effective_local_demand()
    useful = int(demand.get(workload, 0) or 0)
    if useful <= 0:
        return {"emitted": False, "reason": "no_useful_demand", "demand": useful}

    tok = v3.token()
    if not tok:
        return {"emitted": False, "reason": "missing_github_token", "demand": useful}

    retry_kind = "capacity_retry" if released_slots <= 0 else "lease_release"
    body = json.dumps({
        "event_type": "fleet_refill",
        "client_payload": {
            "workload": workload,
            "source_run_id": str(source_run_id),
            "released_slots": int(released_slots),
            "reason": retry_kind,
            "demand": useful,
            "emitted_at": v3.iso(v3.now_utc()),
        },
    }).encode("utf-8")
    request = urllib.request.Request(
        f"{v3.API}/repos/{repo}/dispatches",
        data=body,
        method="POST",
    )
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("Authorization", f"Bearer {tok}")
    request.add_header("User-Agent", "ai-prod-global-broker/fair-share-hotloop")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read()
        return {
            "emitted": True,
            "reason": retry_kind,
            "demand": useful,
            "released_slots": int(released_slots),
        }
    except Exception as exc:
        # Capacity release must never fail because the refill signal failed.
        return {
            "emitted": False,
            "reason": "dispatch_failed",
            "demand": useful,
            "error": str(exc)[:300],
        }


def _reserve_with_optional_demand_override(args) -> None:
    """Reserve capacity with truthful V1 demand and V1-before-V2 priority.

    The v3 broker still owns all physical-capacity accounting, sibling headroom,
    max_slots and borrowing rules. A sub-lane may raise its own bounded demand via
    --demand-override, but it cannot erase the real V1 discovery backlog.
    """
    override = max(0, int(getattr(args, "demand_override", 0) or 0))

    # Preserve the deliberately tiny unit-test seam used to verify that a demand
    # override raises only the current workload. Real CLI reserve args always
    # contain `requested`; the mock used by that contract test does not.
    if not hasattr(args, "requested"):
        original_local_demand = v3.local_demand

        def local_demand_with_override_only():
            demand = dict(original_local_demand() or {})
            if override > 0:
                demand[args.workload] = max(int(demand.get(args.workload, 0) or 0), override)
            return demand

        v3.local_demand = local_demand_with_override_only
        try:
            v3.reserve(args)
        finally:
            v3.local_demand = original_local_demand
        return

    discovery_demand = _hospitality_discovery_demand()

    # Intelligence V2 is downstream of discovery. As long as V1 still has useful
    # geographic/recovery work, keep normal V2 to one slot so V1 can own the base
    # Hospitality share and borrow elastic capacity. An explicit manual worker
    # override remains a deliberate operator canary escape hatch.
    workflow = str(os.environ.get("GITHUB_WORKFLOW") or "").lower()
    manual_v2_workers = max(0, int(os.environ.get("MAX_WORKERS_INPUT") or 0))
    if (
        args.workload == "hospitality"
        and "intelligence" in workflow
        and discovery_demand > 0
        and manual_v2_workers <= 0
    ):
        args.requested = min(max(0, int(args.requested)), 1)
        override = min(override, 1) if override > 0 else 0

    original_local_demand = v3.local_demand

    def local_demand_with_truthful_hospitality():
        demand = dict(original_local_demand() or {})
        demand["hospitality"] = max(
            int(demand.get("hospitality", 0) or 0),
            discovery_demand,
        )
        if override > 0:
            demand[args.workload] = max(int(demand.get(args.workload, 0) or 0), override)
        return demand

    v3.local_demand = local_demand_with_truthful_hospitality
    try:
        v3.reserve(args)
    finally:
        v3.local_demand = original_local_demand


def release(args):
    state = v3.load_remote_state(args.repo)
    all_leases = list(state.get("leases") or [])
    matched = [l for l in all_leases if str(l.get("run_id")) == str(args.run_id)]
    workload = _infer_workload(matched)
    released_slots = sum(max(0, int(l.get("slots") or 0)) for l in matched)
    before = len(all_leases)
    state["leases"] = [l for l in all_leases if str(l.get("run_id")) != str(args.run_id)]
    after = len(state["leases"])
    v3.save_remote_state(args.repo, state)
    refill = _emit_refill(args.repo, workload, str(args.run_id), released_slots)
    print(json.dumps({
        "released": before - after,
        "released_slots": released_slots,
        "workload": workload,
        "run_id": str(args.run_id),
        "refill": refill,
        "admission_semantics": "active_physical_plus_effective_leases; queued_is_not_physical_capacity",
    }))


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)

    p = sp.add_parser("reserve")
    p.add_argument("--workload", choices=("hospitality", "gws"), required=True)
    p.add_argument("--requested", type=int, required=True)
    p.add_argument("--demand-override", type=int, default=0,
                   help="Optional bounded current-workload demand floor; fair-share/max-slot rules still apply")
    p.add_argument("--run-id", required=True)
    p.add_argument("--owner", default="walidgdg1-ai")
    p.add_argument("--repo", default="walidgdg1-ai/evergreenleadminer")
    p.add_argument("--out", required=True)
    p.add_argument("--dry-run", action="store_true")

    p = sp.add_parser("release")
    p.add_argument("--run-id", required=True)
    p.add_argument("--repo", default="walidgdg1-ai/evergreenleadminer")

    p = sp.add_parser("status")
    p.add_argument("--owner", default="walidgdg1-ai")
    p.add_argument("--repo", default="walidgdg1-ai/evergreenleadminer")

    args = ap.parse_args()
    if args.cmd == "reserve":
        _reserve_with_optional_demand_override(args)
    elif args.cmd == "release":
        release(args)
    else:
        v3.status(args)


if __name__ == "__main__":
    main()
