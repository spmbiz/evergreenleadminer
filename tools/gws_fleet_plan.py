#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

HUB_DATASET = "commerces-recenses-par-hubbrussels-vbx"
HUB_URL = f"https://opendata.brussels.be/api/explore/v2.1/catalog/datasets/{HUB_DATASET}/records"


def load_json(path: str | Path, default: Any) -> Any:
    p = Path(path)
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


def iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def http_json(url: str, token: str | None = None, timeout: int = 20) -> dict:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "evergreenleadminer-fleet/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def github_repo_job_counts(repo: str, token: str | None) -> tuple[int, int, list[str]]:
    active = 0
    queued = 0
    errors: list[str] = []
    for status in ("in_progress", "queued"):
        url = f"https://api.github.com/repos/{repo}/actions/runs?status={status}&per_page=100"
        data = None
        for candidate_token in (token, None) if token else (None,):
            try:
                data = http_json(url, candidate_token)
                break
            except Exception as exc:
                if candidate_token is None:
                    errors.append(f"{repo}:{status}:{type(exc).__name__}:{exc}")
        if not data:
            continue
        for run in data.get("workflow_runs", []):
            jobs_url = run.get("jobs_url")
            if not jobs_url:
                if status == "in_progress":
                    active += 1
                else:
                    queued += 1
                continue
            jobs = None
            for candidate_token in (token, None) if token else (None,):
                try:
                    jobs = http_json(jobs_url + ("&" if "?" in jobs_url else "?") + "per_page=100", candidate_token)
                    break
                except Exception:
                    pass
            if not jobs:
                if status == "in_progress":
                    active += 1
                else:
                    queued += 1
                continue
            for job in jobs.get("jobs", []):
                js = job.get("status")
                if js == "in_progress":
                    active += 1
                elif js == "queued":
                    queued += 1
    return active, queued, errors


def fetch_hub_snapshot(outdir: Path) -> dict:
    outdir.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []
    offset = 0
    total = None
    pages = 0
    attempts_total = 0
    t0 = time.time()
    while total is None or offset < total:
        params = urllib.parse.urlencode({"limit": 100, "offset": offset})
        url = HUB_URL + "?" + params
        last = None
        payload = None
        for attempt in range(1, 5):
            attempts_total += 1
            try:
                req = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": "GWS-Brussels-Fleet/1.0",
                        "Accept": "application/json",
                    },
                )
                with urllib.request.urlopen(req, timeout=45) as response:
                    payload = json.load(response)
                break
            except Exception as exc:
                last = exc
                time.sleep(1.25 * attempt)
        if payload is None:
            raise RuntimeError(f"hub snapshot failed at offset {offset}: {last}")
        if total is None:
            total = int(payload.get("total_count", 0))
        batch = payload.get("results", [])
        if not batch and offset < int(total or 0):
            raise RuntimeError(f"empty Hub page before total_count: {offset}/{total}")
        records.extend(batch)
        offset += len(batch)
        pages += 1
        if not batch:
            break
    if total is None or len(records) != total:
        raise RuntimeError(f"Hub integrity mismatch materialized={len(records)} api_total={total}")
    raw_path = outdir / "hub_brussels_current.jsonl"
    h = hashlib.sha256()
    with raw_path.open("w", encoding="utf-8") as fh:
        for row in records:
            line = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            fh.write(line + "\n")
            h.update((line + "\n").encode("utf-8"))
    summary = {
        "dataset": HUB_DATASET,
        "api_total": total,
        "materialized": len(records),
        "pages": pages,
        "attempts": attempts_total,
        "sha256": h.hexdigest(),
        "elapsed_seconds": round(time.time() - t0, 3),
        "fetched_at": iso_now(),
    }
    (outdir / "hub_snapshot_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def build_tasks(workloads: dict, coverage: dict) -> list[dict]:
    now = dt.datetime.now(dt.timezone.utc)
    result: list[dict] = []
    for workload in workloads.get("workloads", []):
        if not workload.get("enabled", False):
            continue
        refresh_hours = float(workload.get("refresh_after_hours", 168))
        for territory in workload.get("territories", []):
            for family in workload.get("families", []):
                task_id = f"{workload['id']}::{territory['name']}::{family['name']}"
                prior = coverage.get("tasks", {}).get(task_id, {})
                last_success = parse_iso(prior.get("last_success"))
                last_status = prior.get("last_status")
                never = last_success is None
                retry = last_status in {"failed_retryable", "error_retryable"}
                stale_hours = 10_000.0 if never else max(0.0, (now - last_success).total_seconds() / 3600.0)
                if not never and not retry and stale_hours < refresh_hours:
                    continue
                base_priority = (
                    float(workload.get("priority", 0))
                    + float(territory.get("priority", 0))
                    + float(family.get("priority", 0))
                )
                yield_signal = prior.get("last_net_new_per_minute")
                if yield_signal is None:
                    yield_signal = prior.get("last_useful_per_minute") or 0
                yield_bonus = min(500.0, float(yield_signal) * 25.0)
                score = base_priority + yield_bonus + min(stale_hours, 5000.0)
                if never:
                    score += 100_000.0
                if retry:
                    score += 50_000.0
                result.append(
                    {
                        "task_id": task_id,
                        "workload_id": workload["id"],
                        "worker": workload.get("worker", "gws_hub_overture"),
                        "source_profile": workload.get("source_profile", "gws_hub_overture"),
                        "territory": territory["name"],
                        "postal_codes_json": json.dumps(territory.get("postal_codes", []), separators=(",", ":")),
                        "family": family["name"],
                        "keywords_json": json.dumps(family.get("keywords", []), separators=(",", ":")),
                        "max_serious": int(workload.get("max_serious_per_task", 300)),
                        "priority_score": round(score, 3),
                        "south": bool(territory.get("south", False)),
                    }
                )
    result.sort(key=lambda x: (-x["priority_score"], x["task_id"]))
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=("github", "circleci"), required=True)
    ap.add_argument("--outdir", default="results/fleet_plan")
    ap.add_argument("--fixed-capacity", type=int, default=None)
    ap.add_argument("--github-output", default=os.getenv("GITHUB_OUTPUT", ""))
    args = ap.parse_args()

    desired = load_json("control/desired_state.json", {})
    gws = load_json("config/gws_fleet.json", {})
    workloads = {"peer_repositories": gws.get("peer_repositories", []), "workloads": [gws]}
    coverage = load_json("state/gws_coverage.json", {"tasks": {}})
    profiles = load_json("state/gws_source_profiles.json", {"profiles": {}})
    pending = load_json("gpt/gws_pending_batches.json", {"pending_records": 0})
    provider_state = load_json("state/provider_capacity.json", {"schema_version": 1})
    provider_doc = load_json("config/providers.json", {"providers": {}})
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    provider_cfg = provider_doc.get("providers", {}).get(args.provider, {})
    enabled = bool(desired.get("enabled") and desired.get("continuous") and gws.get("enabled", True) and provider_cfg.get("enabled"))
    active_jobs = 0
    queued_jobs = 0
    capacity_errors: list[str] = []
    limit = int(args.fixed_capacity or provider_cfg.get("concurrency_limit") or 0)

    if args.provider == "github" and enabled:
        token = os.getenv("GITHUB_TOKEN")
        repo = os.getenv("GITHUB_REPOSITORY", "walidgdg1-ai/evergreenleadminer")
        repos = [repo] + [r for r in workloads.get("peer_repositories", []) if r != repo]
        for candidate in repos:
            a, q, errs = github_repo_job_counts(candidate, token if candidate == repo else None)
            active_jobs += a
            queued_jobs += q
            capacity_errors.extend(errs)

    configured_target = int(provider_cfg.get("worker_target") or limit)
    if str(desired.get("mode", "")).lower() == "maximum":
        target_slots = max(0, min(limit, configured_target))
    else:
        target_slots = max(0, min(configured_target, int(math.floor(limit * float(desired.get("target_utilization", 0.90))))) )
    if args.provider == "github":
        # The planner itself is part of active_jobs but releases its slot before workers start.
        active_other = max(0, active_jobs - 1)
        provider_available = max(0, target_slots - active_other - queued_jobs)
    else:
        provider_available = target_slots

    tasks = build_tasks(workloads, coverage)
    profile_name = tasks[0]["source_profile"] if tasks else "gws_hub_overture"
    profile = profiles.get("profiles", {}).get(profile_name, {})
    stable_parallel = int(profile.get("stable_parallel") or profile.get("initial_parallel") or 4)
    source_cap = int(profile.get("max_parallel") or stable_parallel)
    worker_count = min(provider_available, stable_parallel, source_cap, len(tasks)) if enabled else 0

    credit_guard = None
    if args.provider == "circleci" and enabled:
        month = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")
        cc_state = provider_state.get("circleci", {})
        used = float(cc_state.get("estimated_credits_used") or 0) if cc_state.get("month") == month else 0.0
        budget = float(provider_cfg.get("credit_budget_monthly") or provider_cfg.get("open_source_linux_credits_per_month") or 0)
        guard_ratio = float(provider_cfg.get("credit_guard_ratio") or 0.95)
        guard_limit = budget * guard_ratio
        remaining_guard = max(0.0, guard_limit - used)
        credits_per_minute = float(provider_cfg.get("worker_credits_per_minute") or 10)
        expected_minutes = float(profile.get("expected_worker_minutes") or 20)
        expected_per_worker = max(1.0, credits_per_minute * expected_minutes)
        affordable_workers = int(remaining_guard // expected_per_worker)
        worker_count = min(worker_count, affordable_workers)
        credit_guard = {
            "month": month, "estimated_used": used, "budget": budget,
            "guard_limit": guard_limit, "remaining_guard": remaining_guard,
            "expected_credits_per_worker": expected_per_worker,
            "affordable_workers_this_cycle": affordable_workers,
            "auto_purchase": bool(provider_cfg.get("auto_purchase", False)),
        }

    pending_records = int(pending.get("pending_records") or 0)
    soft = int(desired.get("review_backpressure_soft", 5000))
    hard = int(desired.get("review_backpressure_hard", 20000))
    if pending_records >= hard:
        worker_count = min(worker_count, 2)
    elif pending_records >= soft:
        worker_count = max(1 if worker_count else 0, worker_count // 2)

    selected = tasks[:worker_count]
    lease_seconds = 60 * 50
    now = dt.datetime.now(dt.timezone.utc)
    expires = (now + dt.timedelta(seconds=lease_seconds)).replace(microsecond=0).isoformat()
    for task in selected:
        task["lease_id"] = f"{args.provider}-{uuid.uuid4().hex[:16]}"
        task["lease_expires_at"] = expires

    hub_summary = None
    if selected and any(t.get("worker") == "gws_hub_overture" for t in selected):
        hub_summary = fetch_hub_snapshot(outdir)

    plan = {
        "schema_version": 1,
        "generated_at": iso_now(),
        "provider": args.provider,
        "enabled": enabled,
        "mode": desired.get("mode"),
        "provider_limit": limit,
        "target_slots": target_slots,
        "active_jobs_observed": active_jobs,
        "queued_jobs_observed": queued_jobs,
        "provider_available": provider_available,
        "source_profile": profile_name,
        "source_stable_parallel": stable_parallel,
        "selected_count": len(selected),
        "available_backlog": len(tasks),
        "review_pending_before": pending_records,
        "capacity_errors": capacity_errors[-20:],
        "credit_guard": credit_guard,
        "hub_snapshot": hub_summary,
        "tasks": selected,
    }
    (outdir / "plan.json").write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")

    outputs = {
        "enabled": "true" if enabled else "false",
        "selected_count": str(len(selected)),
        "max_parallel": str(max(1, len(selected))),
        "matrix": json.dumps({"include": selected}, separators=(",", ":")),
    }
    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as fh:
            for key, value in outputs.items():
                fh.write(f"{key}={value}\n")
    print(json.dumps({**plan, "tasks": [t["task_id"] for t in selected]}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
