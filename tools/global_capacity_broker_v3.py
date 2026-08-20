#!/usr/bin/env python3
"""Fair-share account-wide GitHub worker broker v3.

The broker never kills long-running jobs. It observes real occupancy, classifies
legacy/unleased jobs into their workload when possible, protects only the missing
part of each demanding workload's weighted share, and lets any workload borrow
truly idle capacity immediately. Rebalancing happens at natural job completion.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
from collections import Counter
from pathlib import Path
import tempfile
import time
import urllib.parse
import urllib.request

import fleet_runtime as fr

ROOT = Path(__file__).resolve().parents[1]
API = "https://api.github.com"
STATE_TAG = "global-fleet-broker"
STATE_ASSET = "global-capacity.json"
TENDER_REPO = "walidgdg1-ai/tender-engine"
LEASE_LAUNCH_GRACE_SECONDS = 120


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def iso(v: dt.datetime):
    return v.isoformat().replace("+00:00", "Z")


def parse_ts(v):
    try:
        return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:
        return None


def token():
    return os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or os.environ.get("FLEET_GH_TOKEN") or ""


def req(url, method="GET", accept="application/vnd.github+json"):
    r = urllib.request.Request(url, method=method)
    r.add_header("Accept", accept)
    r.add_header("User-Agent", "ai-prod-global-broker/3.1")
    r.add_header("X-GitHub-Api-Version", "2022-11-28")
    if token():
        r.add_header("Authorization", f"Bearer {token()}")
    return r


def api_json(url):
    with urllib.request.urlopen(req(url), timeout=30) as x:
        return json.loads(x.read())


def load_release_asset_json(repo: str, tag: str, asset_name: str, default=None):
    try:
        rel = api_json(f"{API}/repos/{repo}/releases/tags/{urllib.parse.quote(tag, safe='')}")
        asset = next((a for a in rel.get("assets") or [] if a.get("name") == asset_name), None)
        if not asset:
            return default
        with urllib.request.urlopen(
            req(f"{API}/repos/{repo}/releases/assets/{asset['id']}", accept="application/octet-stream"), timeout=30
        ) as x:
            return json.loads(x.read())
    except Exception:
        return default


def load_remote_state(repo: str):
    default = {"schema_version": 3, "leases": [], "updated_at": None}
    return load_release_asset_json(repo, STATE_TAG, STATE_ASSET, default) or default


def save_remote_state(repo: str, state: dict):
    state["updated_at"] = iso(now_utc())
    with tempfile.TemporaryDirectory(prefix="global-fleet-broker-") as td:
        p = Path(td) / STATE_ASSET
        p.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        fr.release_upload(repo, STATE_TAG, str(p))


def _settle_delay(run_id: str, attempt: int) -> float:
    jitter = (sum(ord(ch) for ch in str(run_id)) % 19) / 100.0
    return min(1.5, 0.20 * max(1, attempt) + jitter)


def save_reservation_reconciled(repo: str, intended_state: dict, lease: dict | None, attempts: int = 4):
    """Converge a reservation into the shared release asset.

    GWS and Hospitality planners are intentionally allowed to plan concurrently.
    The release asset is not a compare-and-swap store, so a single blind write can
    lose a sibling lease. Each non-zero reserve therefore performs several
    read/merge/write rounds with deterministic jitter. Once either writer has seen
    the sibling lease, subsequent rounds preserve it. A zero-slot decision does
    not write lease state at all.
    """
    if lease is None:
        return {"confirmed": True, "attempts": 0, "mode": "zero_slot_no_write"}

    run_id = str(lease.get("run_id") or "")
    if not run_id:
        raise RuntimeError("refusing to persist broker lease without run_id")

    rounds = max(3, int(attempts))
    for attempt in range(1, rounds + 1):
        fresh = load_remote_state(repo)
        fresh_leases = prune_leases(fresh)
        merged = [x for x in fresh_leases if str(x.get("run_id") or "") != run_id]
        merged.append(lease)
        candidate = dict(fresh)
        candidate["schema_version"] = 3
        candidate["leases"] = merged
        candidate["last_decision"] = intended_state.get("last_decision")
        save_remote_state(repo, candidate)
        time.sleep(_settle_delay(run_id, attempt))

    final = load_remote_state(repo)
    confirmed = any(
        str(x.get("run_id") or "") == run_id
        and int(x.get("slots") or 0) == int(lease.get("slots") or 0)
        for x in (final.get("leases") or [])
    )
    if not confirmed:
        raise RuntimeError(f"broker reserve reconciliation failed run_id={run_id} rounds={rounds}")
    return {"confirmed": True, "attempts": rounds, "mode": "optimistic_merge_settled"}


def release_lease_reconciled(repo: str, run_id: str, attempts: int = 4):
    """Converge removal of one run lease without dropping concurrent reserves."""
    run_id = str(run_id)
    initial = load_remote_state(repo)
    initial_matches = [x for x in (initial.get("leases") or []) if str(x.get("run_id") or "") == run_id]
    if not initial_matches:
        return {
            "released": 0,
            "released_slots": 0,
            "matched_leases": [],
            "confirmed": True,
            "attempts": 0,
            "mode": "absent_no_write",
        }

    rounds = max(3, int(attempts))
    seen_matches = list(initial_matches)
    for attempt in range(1, rounds + 1):
        fresh = load_remote_state(repo)
        leases = prune_leases(fresh)
        matches = [x for x in leases if str(x.get("run_id") or "") == run_id]
        if matches:
            seen_matches = matches
        fresh["schema_version"] = 3
        fresh["leases"] = [x for x in leases if str(x.get("run_id") or "") != run_id]
        save_remote_state(repo, fresh)
        time.sleep(_settle_delay(run_id, attempt))

    final = load_remote_state(repo)
    if any(str(x.get("run_id") or "") == run_id for x in (final.get("leases") or [])):
        raise RuntimeError(f"broker release reconciliation failed run_id={run_id} rounds={rounds}")
    return {
        "released": len(initial_matches),
        "released_slots": sum(max(0, int(x.get("slots") or 0)) for x in initial_matches),
        "matched_leases": initial_matches,
        "confirmed": True,
        "attempts": rounds,
        "mode": "optimistic_remove_settled",
    }


def useful_hospitality_count():
    try:
        import hospitality_grid_plan as hp
        coverage = (fr.load_json(ROOT / "state/coverage.json", {}).get("shards") or {})
        now = now_utc()
        n = 0
        for s in hp.expanded_catalog():
            c = coverage.get(s["key"]) or {}
            last = parse_ts(c.get("last_success"))
            changed = c.get("release") != s.get("release")
            age = 1e9 if changed or not last else max(0, (now - last).total_seconds() / 3600)
            if changed or not last or age >= 168 or c.get("status") in ("partial", "failed_retryable"):
                n += 1
        return n
    except Exception:
        return 1


def useful_gws_count():
    try:
        import gws_fleet_plan as gp
        gws = gp.load_json(ROOT / "config/gws_fleet.json", {})
        coverage = gp.load_json(ROOT / "state/gws_coverage.json", {"tasks": {}})
        workloads = {"peer_repositories": gws.get("peer_repositories", []), "workloads": [gws]}
        return len(gp.build_tasks(workloads, coverage))
    except Exception:
        return 1


def useful_tender_count():
    status = load_release_asset_json(TENDER_REPO, "fleet-state", "fleet-status-latest.json", None)
    if not isinstance(status, dict):
        return 6
    if status.get("enabled") is False:
        return 0
    backlog = status.get("backlog") or {}
    pending = int(backlog.get("pending_dce_candidates") or 0)
    if pending > 0:
        return pending
    if backlog.get("active_discovery_run_id"):
        return 320
    return 0


def local_demand():
    return {
        "hospitality": useful_hospitality_count(),
        "tenders": useful_tender_count(),
        "gws": useful_gws_count(),
    }


def owner_repos(owner: str):
    urls = []
    if token():
        urls.append(f"{API}/user/repos?affiliation=owner&per_page=100")
    urls.append(f"{API}/users/{urllib.parse.quote(owner)}/repos?type=owner&per_page=100")
    seen = {}
    for url in urls:
        try:
            for r in api_json(url):
                full = r.get("full_name")
                if full:
                    seen[full] = r
            if seen:
                break
        except Exception:
            continue
    return list(seen.values())


def live_jobs(owner: str):
    result = []
    for repo in owner_repos(owner):
        full = repo.get("full_name")
        if not full:
            continue
        runs = []
        seen_run_ids = set()
        for status_name in ("in_progress", "queued"):
            data = None
            try:
                data = api_json(f"{API}/repos/{full}/actions/runs?status={status_name}&per_page=30")
            except Exception:
                try:
                    r = urllib.request.Request(
                        f"{API}/repos/{full}/actions/runs?status={status_name}&per_page=30",
                        headers={"Accept": "application/vnd.github+json", "User-Agent": "ai-prod-global-broker/3.1"},
                    )
                    with urllib.request.urlopen(r, timeout=20) as x:
                        data = json.loads(x.read())
                except Exception:
                    data = None
            for run in (data or {}).get("workflow_runs") or []:
                rid = str(run.get("id") or "")
                if rid and rid not in seen_run_ids:
                    runs.append(run)
                    seen_run_ids.add(rid)
        for run in runs:
            rid = str(run.get("id") or "")
            active = queued = 0
            try:
                jobs = api_json(run["jobs_url"]).get("jobs") or []
                active = sum(j.get("status") == "in_progress" for j in jobs)
                queued = sum(j.get("status") == "queued" for j in jobs)
            except Exception:
                if run.get("status") == "in_progress":
                    active = 1
                elif run.get("status") == "queued":
                    queued = 1
            if active or queued:
                result.append({
                    "repo": full,
                    "run_id": rid,
                    "active_jobs": active,
                    "queued_jobs": queued,
                    "jobs": active + queued,
                    "status": run.get("status"),
                    "workflow_name": run.get("name") or run.get("display_title") or "",
                    "workflow_path": run.get("path") or "",
                })
    return result


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


def prune_leases(state: dict, current_run: str | None = None):
    now = now_utc()
    keep = []
    for l in state.get("leases") or []:
        exp = parse_ts(l.get("expires_at"))
        if exp and exp > now and (not current_run or str(l.get("run_id")) != str(current_run)):
            keep.append(l)
    state["leases"] = keep
    return keep


def effective_lease_accounting(leases: list[dict], jobs: list[dict]):
    live_by_run = {str(j.get("run_id") or ""): j for j in jobs}
    now = now_utc()
    total = 0
    by_workload = Counter()
    details = []
    for lease in leases:
        rid = str(lease.get("run_id") or "")
        reserved = max(0, int(lease.get("slots") or 0))
        created = parse_ts(lease.get("created_at"))
        age_seconds = max(0.0, (now - created).total_seconds()) if created else 0.0
        live = live_by_run.get(rid)
        effective = reserved
        reason = "full_reservation_launch_grace"
        if age_seconds >= LEASE_LAUNCH_GRACE_SECONDS:
            if live is None:
                effective = 0
                reason = "reclaimed_no_live_outstanding"
            else:
                outstanding = max(0, int(live.get("active_jobs") or 0) + int(live.get("queued_jobs") or 0))
                effective = min(reserved, outstanding)
                reason = "shrunk_to_live_outstanding" if effective < reserved else "live_outstanding_matches_reservation"
        total += effective
        workload = str(lease.get("workload") or "")
        by_workload[workload] += effective
        details.append({
            "run_id": rid,
            "workload": workload,
            "reserved_slots": reserved,
            "effective_slots": effective,
            "age_seconds": round(age_seconds, 1),
            "observed_active": int((live or {}).get("active_jobs") or 0),
            "observed_queued": int((live or {}).get("queued_jobs") or 0),
            "reason": reason,
        })
    return total, by_workload, details


def fair_target(total: int, demand: int, cfg: dict) -> int:
    if demand <= 0 or not cfg.get("enabled", True):
        return 0
    floor = max(0, int(cfg.get("min_slots_when_demanding") or 0))
    max_slots = max(0, int(cfg.get("max_slots") or total))
    weighted = int(math.floor(total * max(0.0, float(cfg.get("weight") or 0))))
    return min(demand, max_slots, max(floor, weighted))


def reserve(args):
    cfg = fr.load_json(ROOT / "config/global_fleet.json", {})
    gh = cfg.get("github") or {}
    workloads = cfg.get("workloads") or {}
    total = int(gh.get("capacity") or 60)
    ttl = int(gh.get("lease_ttl_minutes") or 55)

    state = load_remote_state(args.repo)
    leases = prune_leases(state, args.run_id)
    lease_run_ids = {str(x.get("run_id")) for x in leases}
    jobs = live_jobs(args.owner)
    external_jobs = [j for j in jobs if j["run_id"] != str(args.run_id) and j["run_id"] not in lease_run_ids]
    for j in external_jobs:
        j["workload"] = classify_job(j, workloads)

    external_slots = sum(int(j.get("active_jobs") or 0) for j in external_jobs)
    external_queued = sum(int(j.get("queued_jobs") or 0) for j in external_jobs)
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
            "created_at": iso(now_utc()),
            "expires_at": iso(now_utc() + dt.timedelta(minutes=ttl)),
        }
        leases.append(lease)

    decision = {
        "at": iso(now_utc()),
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
    persistence = {"confirmed": True, "attempts": 0, "mode": "dry_run"}
    if not args.dry_run:
        persistence = save_reservation_reconciled(args.repo, state, lease)
    payload = dict(decision)
    payload.update({"lease": lease, "dry_run": bool(args.dry_run), "reservation_persistence": persistence})
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))


def release(args):
    result = release_lease_reconciled(args.repo, args.run_id)
    print(json.dumps({
        "released": int(result.get("released") or 0),
        "released_slots": int(result.get("released_slots") or 0),
        "run_id": str(args.run_id),
        "release_persistence": {
            "confirmed": bool(result.get("confirmed")),
            "attempts": int(result.get("attempts") or 0),
            "mode": result.get("mode"),
        },
    }))


def status(args):
    state = load_remote_state(args.repo)
    prune_leases(state)
    jobs = live_jobs(args.owner)
    cfg = fr.load_json(ROOT / "config/global_fleet.json", {})
    workloads = cfg.get("workloads") or {}
    for j in jobs:
        j["workload"] = classify_job(j, workloads)
    state["live_jobs"] = jobs
    state["demand"] = local_demand()
    state["effective_lease_accounting"] = effective_lease_accounting(state.get("leases") or [], jobs)[2]
    print(json.dumps(state, indent=2))


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("reserve")
    p.add_argument("--workload", choices=("hospitality", "gws"), required=True)
    p.add_argument("--requested", type=int, required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--owner", default="spmbiz")
    p.add_argument("--repo", default="spmbiz/evergreenleadminer")
    p.add_argument("--out", required=True)
    p.add_argument("--dry-run", action="store_true")
    p = sp.add_parser("release")
    p.add_argument("--run-id", required=True)
    p.add_argument("--repo", default="spmbiz/evergreenleadminer")
    p = sp.add_parser("status")
    p.add_argument("--owner", default="spmbiz")
    p.add_argument("--repo", default="spmbiz/evergreenleadminer")
    a = ap.parse_args()
    if a.cmd == "reserve":
        reserve(a)
    elif a.cmd == "release":
        release(a)
    else:
        status(a)


if __name__ == "__main__":
    main()
