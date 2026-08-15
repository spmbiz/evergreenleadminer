#!/usr/bin/env python3
"""Fair-share wrapper around the durable account-wide GitHub capacity broker.

Goals:
- never kill long-running workloads merely because they are old;
- classify unleased legacy/auxiliary jobs into hospitality/GWS/tenders when possible;
- protect a weighted fair-share + minimum floor for every workload that has demand;
- let any workload borrow truly idle capacity immediately;
- cap new allocations by the workload's max including already-running unleased jobs;
- keep the original durable lease state and release semantics.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
from collections import Counter
from pathlib import Path

import global_capacity_broker as base


def classify_job(job: dict, workloads: dict) -> str:
    repo = str(job.get("repo") or "").lower()
    name = str(job.get("workflow_name") or "").lower()
    path = str(job.get("workflow_path") or "").lower()
    blob = f"{name} {path}"

    tender_repo = str((workloads.get("tenders") or {}).get("repo") or "").lower()
    if tender_repo and repo == tender_repo:
        return "tenders"
    if "gws" in blob or "no-website" in blob or "no website" in blob:
        return "gws"
    if "hospitality" in blob or "airbnb" in blob or "property global" in blob:
        return "hospitality"
    if "tender" in blob:
        return "tenders"
    return "external"


def enrich_live_jobs(owner: str) -> list[dict]:
    jobs = base.live_jobs(owner)
    for job in jobs:
        try:
            meta = base.api_json(
                f"{base.API}/repos/{job['repo']}/actions/runs/{job['run_id']}"
            )
        except Exception:
            meta = {}
        job["workflow_name"] = str(meta.get("name") or meta.get("display_title") or "")
        job["workflow_path"] = str(meta.get("path") or "")
    return jobs


def fair_target(total: int, demand: int, cfg: dict) -> int:
    if demand <= 0 or not cfg.get("enabled", True):
        return 0
    floor = max(0, int(cfg.get("min_slots_when_demanding") or 0))
    max_slots = max(0, int(cfg.get("max_slots") or total))
    weighted = int(math.floor(total * max(0.0, float(cfg.get("weight") or 0))))
    return min(demand, max_slots, max(floor, weighted))


def reserve(args) -> None:
    cfg = base.fr.load_json(base.ROOT / "config/global_fleet.json", {})
    gh = cfg.get("github") or {}
    workloads = cfg.get("workloads") or {}
    total = int(gh.get("capacity") or 20)
    ttl = int(gh.get("lease_ttl_minutes") or 55)

    state = base.load_remote_state(args.repo)
    leases = base.prune_leases(state, args.run_id)
    lease_run_ids = {str(x.get("run_id")) for x in leases}

    jobs = enrich_live_jobs(args.owner)
    external_jobs = [
        j for j in jobs
        if str(j.get("run_id")) != str(args.run_id)
        and str(j.get("run_id")) not in lease_run_ids
    ]
    for j in external_jobs:
        j["workload"] = classify_job(j, workloads)

    external_slots = sum(int(j.get("active_jobs") or 0) for j in external_jobs)
    external_queued = sum(int(j.get("queued_jobs") or 0) for j in external_jobs)

    leased_slots, leased_by_workload, lease_accounting = base.effective_lease_accounting(leases, jobs)
    active_by_workload = Counter()
    queued_by_workload = Counter()
    for j in external_jobs:
        workload = str(j.get("workload") or "external")
        active_by_workload[workload] += int(j.get("active_jobs") or 0)
        queued_by_workload[workload] += int(j.get("queued_jobs") or 0)

    demand = base.local_demand()
    targets = {
        name: fair_target(total, int(demand.get(name, 0) or 0), wcfg)
        for name, wcfg in workloads.items()
        if wcfg.get("enabled", True)
    }

    free_before_headroom = max(0, total - external_slots - leased_slots)
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
                "queued_runnable": queued,
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
            "created_at": base.iso(base.now_utc()),
            "expires_at": base.iso(base.now_utc() + dt.timedelta(minutes=ttl)),
        }
        leases.append(lease)

    decision = {
        "at": base.iso(base.now_utc()),
        "broker_version": 3,
        "workload": args.workload,
        "requested": requested,
        "allocated": slots,
        "capacity": total,
        "external_slots": external_slots,
        "external_queued": external_queued,
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
    }

    state["schema_version"] = 3
    state["leases"] = leases
    state["last_decision"] = decision
    if not args.dry_run:
        base.save_remote_state(args.repo, state)

    payload = dict(decision)
    payload.update({"lease": lease, "dry_run": bool(args.dry_run)})
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))


def main() -> None:
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
        base.release(a)
    else:
        base.status(a)


if __name__ == "__main__":
    main()
