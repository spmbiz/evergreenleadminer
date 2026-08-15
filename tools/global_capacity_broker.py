#!/usr/bin/env python3
"""Account-wide GitHub worker broker for AI Prod harvest fleets.

The broker prevents a planner race where multiple autonomous workloads observe the
same free GitHub slots and all claim them. Planner jobs using this broker must
share one GitHub Actions concurrency group. Reservations are persisted as a tiny
GitHub Release asset and expire automatically if a workflow dies.

Current scope:
- hospitality and GWS consume explicit broker leases;
- tender-engine and other owner repos are observed as external account demand;
- CircleCI has its own provider pool and is not counted in GitHub's 20 slots.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
from pathlib import Path
import tempfile
import urllib.error
import urllib.parse
import urllib.request

import fleet_runtime as fr

ROOT = Path(__file__).resolve().parents[1]
API = "https://api.github.com"
STATE_TAG = "global-fleet-broker"
STATE_ASSET = "global-capacity.json"


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
    r.add_header("X-GitHub-Api-Version", "2022-11-28")
    if token():
        r.add_header("Authorization", f"Bearer {token()}")
    return r


def api_json(url):
    with urllib.request.urlopen(req(url), timeout=30) as x:
        return json.loads(x.read())


def load_remote_state(repo: str):
    default = {"schema_version": 1, "leases": [], "updated_at": None}
    try:
        rel = api_json(f"{API}/repos/{repo}/releases/tags/{urllib.parse.quote(STATE_TAG, safe='')}")
    except Exception:
        return default
    asset = next((a for a in rel.get("assets") or [] if a.get("name") == STATE_ASSET), None)
    if not asset:
        return default
    try:
        with urllib.request.urlopen(req(f"{API}/repos/{repo}/releases/assets/{asset['id']}", accept="application/octet-stream"), timeout=30) as x:
            return json.loads(x.read())
    except Exception:
        return default


def save_remote_state(repo: str, state: dict):
    state["updated_at"] = iso(now_utc())
    with tempfile.TemporaryDirectory(prefix="global-fleet-broker-") as td:
        p = Path(td) / STATE_ASSET
        p.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        fr.release_upload(repo, STATE_TAG, str(p))


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


def local_demand():
    return {"hospitality": useful_hospitality_count(), "gws": useful_gws_count()}


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
    """Return job demand keyed by workflow run id across visible owner repos."""
    result = []
    for repo in owner_repos(owner):
        full = repo.get("full_name")
        if not full:
            continue
        runs = []
        for status in ("in_progress", "queued"):
            data = None
            for auth in (True, False) if token() else (False,):
                try:
                    url = f"{API}/repos/{full}/actions/runs?status={status}&per_page=30"
                    if auth:
                        data = api_json(url)
                    else:
                        r = urllib.request.Request(url, headers={"Accept":"application/vnd.github+json","User-Agent":"ai-prod-global-broker/1.0"})
                        with urllib.request.urlopen(r, timeout=20) as x:
                            data = json.loads(x.read())
                    break
                except Exception:
                    pass
            for run in (data or {}).get("workflow_runs") or []:
                runs.append(run)
        for run in runs:
            rid = str(run.get("id") or "")
            count = 0
            try:
                jobs = api_json(run["jobs_url"]).get("jobs") or []
                count = sum(j.get("status") in ("in_progress", "queued") for j in jobs)
            except Exception:
                count = 1
            if count:
                result.append({"repo": full, "run_id": rid, "jobs": count, "status": run.get("status")})
    return result


def prune_leases(state: dict, current_run: str | None = None):
    now = now_utc()
    keep = []
    for l in state.get("leases") or []:
        exp = parse_ts(l.get("expires_at"))
        if exp and exp > now and (not current_run or str(l.get("run_id")) != str(current_run)):
            keep.append(l)
    state["leases"] = keep
    return keep


def reserve(args):
    cfg = fr.load_json(ROOT / "config/global_fleet.json", {})
    gh = cfg.get("github") or {}
    wc = cfg.get("workloads") or {}
    total = int(gh.get("capacity") or 20)
    ttl = int(gh.get("lease_ttl_minutes") or 55)
    state = load_remote_state(args.repo)
    leases = prune_leases(state, args.run_id)
    lease_run_ids = {str(x.get("run_id")) for x in leases}
    jobs = live_jobs(args.owner)
    external_jobs = [j for j in jobs if j["run_id"] != str(args.run_id) and j["run_id"] not in lease_run_ids]
    external_slots = sum(int(j.get("jobs") or 0) for j in external_jobs)
    leased_slots = sum(int(l.get("slots") or 0) for l in leases)

    demand = local_demand()
    current_cfg = wc.get(args.workload) or {}
    max_slots = int(current_cfg.get("max_slots") or total)
    free_before_headroom = max(0, total - external_slots - leased_slots)

    # Protect the larger of a sibling's minimum floor or weighted fair share when
    # that sibling has useful backlog but has not acquired its lease yet. This
    # keeps schedule ordering from deciding the whole account allocation.
    sibling_headroom = 0
    active_lease_workloads = {l.get("workload") for l in leases}
    for name, scfg in wc.items():
        if name == args.workload or scfg.get("mode") == "external-observed" or not scfg.get("enabled", True):
            continue
        sibling_demand = int(demand.get(name, 0) or 0)
        if sibling_demand <= 0 or name in active_lease_workloads:
            continue
        floor = int(scfg.get("min_slots_when_demanding") or 0)
        weight = float(scfg.get("weight") or 0)
        weighted = int(math.floor(max(0, total - external_slots) * weight))
        sibling_max = int(scfg.get("max_slots") or total)
        reserve = min(sibling_demand, sibling_max, max(floor, weighted))
        sibling_headroom += max(0, reserve)
    sibling_headroom = min(sibling_headroom, free_before_headroom)

    allocatable = max(0, free_before_headroom - sibling_headroom)
    requested = max(0, int(args.requested))
    slots = min(requested, max_slots, allocatable)
    current_demand = int(demand.get(args.workload, requested) or 0)
    slots = min(slots, current_demand) if current_demand > 0 else 0

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
    state["leases"] = leases
    state["last_decision"] = {
        "at": iso(now_utc()),
        "workload": args.workload,
        "requested": requested,
        "allocated": slots,
        "capacity": total,
        "external_slots": external_slots,
        "leased_other_slots": leased_slots,
        "sibling_headroom": sibling_headroom,
        "demand": demand,
        "external_jobs": external_jobs,
    }
    if not args.dry_run:
        save_remote_state(args.repo, state)
    payload = {
        "allocated": slots,
        "requested": requested,
        "capacity": total,
        "external_slots": external_slots,
        "leased_other_slots": leased_slots,
        "sibling_headroom": sibling_headroom,
        "demand": demand,
        "lease": lease,
        "external_jobs": external_jobs,
        "dry_run": bool(args.dry_run),
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))


def release(args):
    state = load_remote_state(args.repo)
    before = len(state.get("leases") or [])
    state["leases"] = [l for l in state.get("leases") or [] if str(l.get("run_id")) != str(args.run_id)]
    after = len(state["leases"])
    save_remote_state(args.repo, state)
    print(json.dumps({"released": before - after, "run_id": str(args.run_id)}))


def status(args):
    state = load_remote_state(args.repo)
    prune_leases(state)
    state["live_jobs"] = live_jobs(args.owner)
    state["demand"] = local_demand()
    print(json.dumps(state, indent=2))


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
    if a.cmd == "reserve": reserve(a)
    elif a.cmd == "release": release(a)
    else: status(a)


if __name__ == "__main__":
    main()
