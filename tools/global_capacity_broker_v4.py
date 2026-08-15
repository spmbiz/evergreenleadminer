#!/usr/bin/env python3
"""Fair-share GitHub capacity broker v4.

V4 keeps v3's no-preemption policy but treats already queued jobs as committed
capacity. That prevents a workload from pre-booking the next free slots while a
demanding sibling is below its fair share. Long-running jobs are never killed;
rebalancing is admission-only and happens as capacity naturally frees.

Release-driven refill is deliberately emitted from the broker rather than relying
on arbitrarily deep workflow_run chains. A fleet that actually held a lease emits
one repository_dispatch event after releasing it; a tiny controller then starts
the next run if no successor already exists.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import urllib.request
from collections import Counter
from pathlib import Path

import fleet_runtime as fr
from global_capacity_broker_v3 import (
    ROOT,
    api_json,
    classify_job,
    effective_lease_accounting,
    fair_target,
    iso,
    live_jobs,
    load_remote_state,
    local_demand,
    now_utc,
    prune_leases,
    save_remote_state,
    status,
    token,
)


def reserve(args):
    cfg = fr.load_json(ROOT / "config/global_fleet.json", {})
    gh = cfg.get("github") or {}
    workloads = cfg.get("workloads") or {}
    total = int(gh.get("capacity") or 20)
    ttl = int(gh.get("lease_ttl_minutes") or 55)

    state = load_remote_state(args.repo)
    leases = prune_leases(state, args.run_id)
    lease_run_ids = {str(x.get("run_id")) for x in leases}
    jobs = live_jobs(args.owner)
    external_jobs = [j for j in jobs if j["run_id"] != str(args.run_id) and j["run_id"] not in lease_run_ids]
    for j in external_jobs:
        j["workload"] = classify_job(j, workloads)

    external_active = sum(int(j.get("active_jobs") or 0) for j in external_jobs)
    external_queued = sum(int(j.get("queued_jobs") or 0) for j in external_jobs)
    external_committed = external_active + external_queued
    leased_slots, leased_by_workload, lease_accounting = effective_lease_accounting(leases, jobs)

    active_by_workload = Counter()
    queued_by_workload = Counter()
    for j in external_jobs:
        w = str(j.get("workload") or "external")
        active_by_workload[w] += int(j.get("active_jobs") or 0)
        queued_by_workload[w] += int(j.get("queued_jobs") or 0)

    demand = local_demand()
    targets = {
        name: fair_target(total, int(demand.get(name, 0) or 0), wcfg)
        for name, wcfg in workloads.items() if wcfg.get("enabled", True)
    }

    # Core v4 change: queued jobs are already promised future capacity. Counting
    # them here stops new runs from racing those queued jobs for the same slot.
    free_before_headroom = max(0, total - external_committed - leased_slots)
    sibling_headroom = 0
    sibling_reservations = {}
    borrowed_idle_shares = {}
    for name, scfg in workloads.items():
        if name == args.workload or not scfg.get("enabled", True):
            continue
        sibling_demand = int(demand.get(name, 0) or 0)
        target = int(targets.get(name, 0) or 0)
        active = int(active_by_workload.get(name, 0))
        leased = int(leased_by_workload.get(name, 0))
        queued = int(queued_by_workload.get(name, 0))
        committed = active + leased + queued
        missing = max(0, target - committed)
        if sibling_demand <= 0 or target <= 0:
            borrowed_idle_shares[name] = {
                "demand": sibling_demand,
                "target": target,
                "borrowable": int(scfg.get("min_slots_when_demanding") or 0),
                "reason": "no current demand; share is fully borrowable",
            }
            continue
        if missing:
            sibling_reservations[name] = {
                "demand": sibling_demand,
                "target": target,
                "active_unleased": active,
                "leased_effective": leased,
                "queued_committed": queued,
                "committed": committed,
                "missing_headroom": missing,
            }
            sibling_headroom += missing
    sibling_headroom = min(sibling_headroom, free_before_headroom)

    allocatable = max(0, free_before_headroom - sibling_headroom)
    requested = max(0, int(args.requested))
    current_cfg = workloads.get(args.workload) or {}
    current_max = int(current_cfg.get("max_slots") or total)
    current_active = int(active_by_workload.get(args.workload, 0))
    current_leased = int(leased_by_workload.get(args.workload, 0))
    current_queued = int(queued_by_workload.get(args.workload, 0))
    current_committed = current_active + current_leased + current_queued
    current_room = max(0, current_max - current_committed)
    current_demand = int(demand.get(args.workload, requested) or 0)
    unmet_demand = max(0, current_demand - current_committed)
    slots = min(requested, allocatable, current_room, unmet_demand)

    lease = None
    if slots > 0:
        lease = {
            "lease_id": f"github-{args.workload}-{args.run_id}",
            "provider": "github",
            "workload": args.workload,
            "repo": args.repo,
            "run_id": str(args.run_id),
            "slots": slots,
            "created_at": iso(now_utc()),
            "expires_at": iso(now_utc() + dt.timedelta(minutes=ttl)),
        }
        leases.append(lease)

    decision = {
        "at": iso(now_utc()),
        "broker_version": 4,
        "workload": args.workload,
        "requested": requested,
        "allocated": slots,
        "capacity": total,
        "external_slots": external_active,
        "external_queued": external_queued,
        "external_committed_slots": external_committed,
        "leased_other_slots": leased_slots,
        "lease_accounting": lease_accounting,
        "active_by_workload": dict(active_by_workload),
        "queued_by_workload": dict(queued_by_workload),
        "fair_targets": targets,
        "current_committed": current_committed,
        "current_room": current_room,
        "sibling_headroom": sibling_headroom,
        "sibling_reservations": sibling_reservations,
        "borrowed_idle_shares": borrowed_idle_shares,
        "demand": demand,
        "external_jobs": external_jobs,
        "admission_policy": "active_plus_queued_plus_leases; no preemption",
    }
    state["schema_version"] = 4
    state["leases"] = leases
    state["last_decision"] = decision
    if not args.dry_run:
        save_remote_state(args.repo, state)
    payload = dict(decision)
    payload.update({"lease": lease, "dry_run": bool(args.dry_run)})
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))


def _emit_release_refill(repo: str, workload: str, source_run_id: str, released_slots: int):
    """Emit a repository_dispatch refill signal after a real lease is released.

    repository_dispatch is intentionally used because GitHub allows it to trigger
    a new workflow when emitted with GITHUB_TOKEN, while ordinary generated events
    are recursion-suppressed. The refill controller excludes source_run_id from its
    active-run check so the final release job does not block its own successor.
    """
    if workload not in {"hospitality", "gws"} or released_slots <= 0:
        return {"emitted": False, "reason": "no_real_fleet_lease"}

    desired = fr.load_json(ROOT / "control/desired_state.json", {})
    fleet_cfg = fr.load_json(ROOT / "config/global_fleet.json", {})
    workload_cfg = (fleet_cfg.get("workloads") or {}).get(workload) or {}
    if not bool(desired.get("enabled", True)) or not bool(desired.get("continuous", True)):
        return {"emitted": False, "reason": "continuous_fleet_disabled"}
    if not bool(workload_cfg.get("enabled", True)):
        return {"emitted": False, "reason": "workload_disabled"}

    tok = token()
    if not tok:
        return {"emitted": False, "reason": "missing_github_token"}

    body = json.dumps({
        "event_type": "fleet_refill",
        "client_payload": {
            "workload": workload,
            "source_run_id": str(source_run_id),
            "released_slots": int(released_slots),
            "emitted_at": iso(now_utc()),
        },
    }).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/dispatches",
        data=body,
        method="POST",
    )
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("Authorization", f"Bearer {tok}")
    request.add_header("User-Agent", "ai-prod-global-broker/4.1")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read()
        return {"emitted": True, "reason": "release_driven_refill"}
    except Exception as exc:
        # Release must never fail because the refill signal failed. The existing
        # 5-minute watchdog remains a safety net for this exact path.
        return {"emitted": False, "reason": "dispatch_failed", "error": str(exc)[:300]}


def release(args):
    state = load_remote_state(args.repo)
    all_leases = list(state.get("leases") or [])
    matched = [l for l in all_leases if str(l.get("run_id")) == str(args.run_id)]
    workload = str(next((l.get("workload") for l in matched if l.get("workload")), ""))
    released_slots = sum(max(0, int(l.get("slots") or 0)) for l in matched)
    before = len(all_leases)
    state["leases"] = [l for l in all_leases if str(l.get("run_id")) != str(args.run_id)]
    after = len(state["leases"])
    save_remote_state(args.repo, state)
    refill = _emit_release_refill(args.repo, workload, str(args.run_id), released_slots)
    print(json.dumps({
        "released": before - after,
        "released_slots": released_slots,
        "workload": workload,
        "run_id": str(args.run_id),
        "refill": refill,
    }))


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("reserve")
    p.add_argument("--workload", choices=("hospitality", "gws"), required=True)
    p.add_argument("--requested", type=int, required=True)
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
    a = ap.parse_args()
    if a.cmd == "reserve":
        reserve(a)
    elif a.cmd == "release":
        release(a)
    else:
        status(a)


if __name__ == "__main__":
    main()
