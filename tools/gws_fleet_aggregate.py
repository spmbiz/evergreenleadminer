#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
import shutil
from pathlib import Path
from statistics import mean
from typing import Any


def load_json(path: str | Path, default: Any) -> Any:
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def dump_json(path: str | Path, value: Any) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(p)


def append_jsonl(path: str | Path, rows: list[dict]) -> None:
    if not rows:
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def fingerprint(row: dict) -> str:
    keep = {
        "hub_name": row.get("hub_name"),
        "hub_address": row.get("hub_address"),
        "hub_postalcode": row.get("hub_postalcode"),
        "overture_id": row.get("overture_id"),
        "overture_phone": row.get("overture_phone"),
        "overture_email": row.get("overture_email"),
        "overture_websites": row.get("overture_websites"),
        "owned_website": row.get("owned_website"),
        "overture_socials": row.get("overture_socials"),
        "overture_brand": row.get("overture_brand"),
        "outcome": row.get("outcome"),
        "reason": row.get("reason"),
    }
    raw = json.dumps(keep, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def safe_run_id() -> str:
    rid = os.getenv("GITHUB_RUN_ID") or os.getenv("CIRCLE_WORKFLOW_ID") or dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    attempt = os.getenv("GITHUB_RUN_ATTEMPT")
    return f"{rid}-{attempt}" if attempt else str(rid)


def discover_worker_dirs(root: Path) -> list[Path]:
    found = []
    for metrics in root.rglob("metrics.json"):
        if "fleet_plan" in metrics.parts:
            continue
        if (metrics.parent / "checkpoint.json").exists():
            found.append(metrics.parent)
    return sorted(set(found))


def persist_source_snapshot(plan_dir: Path, plan: dict, source_state: dict) -> dict:
    snap = plan.get("hub_snapshot") or {}
    sha = snap.get("sha256")
    if not sha:
        return {"changed": False, "sha256": None}
    previous = source_state.setdefault("sources", {}).get("hub_brussels", {})
    src = plan_dir / "hub_brussels_current.jsonl"
    changed = previous.get("sha256") != sha
    dest = None
    if changed and src.exists():
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = Path("data/gws/source") / f"hub_brussels_{stamp}_{sha[:12]}.jsonl.gz"
        dest.parent.mkdir(parents=True, exist_ok=True)
        with src.open("rb") as in_f, gzip.open(dest, "wb", compresslevel=6) as out_f:
            shutil.copyfileobj(in_f, out_f)
    source_state["sources"]["hub_brussels"] = {
        "sha256": sha,
        "last_seen": iso_now(),
        "last_changed": iso_now() if changed else previous.get("last_changed"),
        "materialized": snap.get("materialized"),
        "api_total": snap.get("api_total"),
        "persisted_snapshot": str(dest) if dest else previous.get("persisted_snapshot"),
    }
    return {"changed": changed, "sha256": sha, "path": str(dest) if dest else None}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=("github", "circleci"), required=True)
    ap.add_argument("--plan-dir", default="results/fleet_plan")
    ap.add_argument("--shards-root", default="results/shards")
    ap.add_argument("--run-id", default="")
    args = ap.parse_args()

    now = iso_now()
    run_id = args.run_id or safe_run_id()
    plan_dir = Path(args.plan_dir)
    plan = load_json(plan_dir / "plan.json", {})
    desired = load_json("control/desired_state.json", {})
    coverage = load_json("state/gws_coverage.json", {"schema_version": 1, "tasks": {}})
    entity_index = load_json("state/gws_entity_index.json", {})
    source_state = load_json("state/gws_source_state.json", {"schema_version": 1, "sources": {}})
    profiles = load_json("state/gws_source_profiles.json", {"schema_version": 1, "profiles": {}})
    pending = load_json("gpt/gws_pending_batches.json", {"schema_version": 1, "batches": [], "pending_records": 0})
    previous_metrics = load_json("metrics/gws_latest.json", {})

    task_by_id = {t.get("task_id"): t for t in plan.get("tasks", [])}
    worker_dirs = discover_worker_dirs(Path(args.shards_root))
    worker_metrics: list[dict] = []
    all_records: list[dict] = []
    for wd in worker_dirs:
        m = load_json(wd / "metrics.json", {})
        if m:
            worker_metrics.append(m)
        rp = wd / "records.jsonl"
        if rp.exists():
            with rp.open("r", encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        all_records.append(json.loads(line))

    date = dt.datetime.now(dt.timezone.utc).date().isoformat()
    observations: list[dict] = []
    changes: list[dict] = []
    review_rows: list[dict] = []
    duplicate_unchanged = 0
    new_entities = 0
    changed_entities = 0
    net_new_by_task: dict[str, int] = {}

    for row in all_records:
        key = row.get("record_key")
        if not key:
            continue
        fp = fingerprint(row)
        prior = entity_index.get(key)
        envelope = {
            **row,
            "observed_at": now,
            "provider": args.provider,
            "fleet_run_id": run_id,
            "fingerprint": fp,
        }
        is_new = prior is None
        is_changed = prior is not None and prior.get("fingerprint") != fp
        if is_new:
            new_entities += 1
            net_new_by_task[row.get("task_id") or ""] = net_new_by_task.get(row.get("task_id") or "", 0) + 1
            observations.append(envelope)
        elif is_changed:
            changed_entities += 1
            net_new_by_task[row.get("task_id") or ""] = net_new_by_task.get(row.get("task_id") or "", 0) + 1
            changes.append({
                **envelope,
                "previous_fingerprint": prior.get("fingerprint"),
                "previous_outcome": prior.get("outcome"),
            })
        else:
            duplicate_unchanged += 1

        if (is_new or is_changed) and row.get("needs_gpt_review"):
            review_rows.append(envelope)

        entity_index[key] = {
            "fingerprint": fp,
            "last_seen": now,
            "first_seen": prior.get("first_seen") if prior else now,
            "outcome": row.get("outcome"),
            "task_id": row.get("task_id"),
            "hub_name": row.get("hub_name"),
            "hub_address": row.get("hub_address"),
            "owned_website": row.get("owned_website"),
        }

    append_jsonl(Path("data/gws/observations") / f"{date}.jsonl", observations)
    append_jsonl(Path("data/gws/changes") / f"{date}.jsonl", changes)

    batch_path = None
    if review_rows:
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
        batch_path = Path("gpt/gws_review") / f"{stamp}_{run_id}.jsonl"
        append_jsonl(batch_path, review_rows)
        pending.setdefault("batches", []).append({
            "batch": str(batch_path),
            "created_at": now,
            "records": len(review_rows),
            "status": "pending",
            "reviewed_at": None,
            "provider": args.provider,
            "fleet_run_id": run_id,
        })
        pending["pending_records"] = int(pending.get("pending_records") or 0) + len(review_rows)

    # Coverage/leases are committed only by this single writer.
    successful = 0
    failed = 0
    for m in worker_metrics:
        task_id = m.get("task_id")
        if not task_id:
            continue
        task = task_by_id.get(task_id, {})
        cov = coverage.setdefault("tasks", {}).setdefault(task_id, {})
        elapsed_seconds = float(m.get("elapsed_seconds") or 0)
        task_net_new = int(net_new_by_task.get(task_id, 0))
        cov.update({
            "territory": task.get("territory") or m.get("territory"),
            "family": task.get("family") or m.get("family"),
            "last_attempt": now,
            "last_status": m.get("status"),
            "last_records_materialized": m.get("records_materialized", 0),
            "last_review_candidates": m.get("review_candidates", 0),
            "last_rejects": m.get("owned_site_or_chain_rejects", 0),
            "last_uncertain": m.get("uncertain", 0),
            "last_elapsed_seconds": m.get("elapsed_seconds", 0),
            "last_useful_per_minute": m.get("useful_per_minute", 0),
            "last_net_new": task_net_new,
            "last_net_new_per_minute": round(task_net_new / max(0.001, elapsed_seconds) * 60.0, 4),
            "last_lease_id": m.get("lease_id"),
            "lease_expires_at": None,
        })
        if m.get("status") == "completed":
            successful += 1
            cov["last_success"] = now
        else:
            failed += 1
            cov["last_error"] = m.get("error")

    selected = int(plan.get("selected_count") or 0)
    missing = max(0, selected - len(worker_metrics))
    failed += missing
    error_rate = failed / max(1, selected)
    errors_text = "\n".join(str(m.get("error") or "") for m in worker_metrics).lower()
    rate_429 = errors_text.count("429") / max(1, selected)
    useful_rates = [float(m.get("useful_per_minute") or 0) for m in worker_metrics if m.get("status") == "completed"]
    useful_total = sum(int(m.get("review_candidates") or 0) for m in worker_metrics)
    net_review_added = len(review_rows)
    worker_minutes = sum(float(m.get("elapsed_seconds") or 0) for m in worker_metrics) / 60.0
    useful_per_worker_minute = useful_total / max(0.001, worker_minutes)
    net_review_per_worker_minute = net_review_added / max(0.001, worker_minutes)

    profile_name = plan.get("source_profile") or "gws_hub_overture"
    profile = profiles.setdefault("profiles", {}).setdefault(profile_name, {})
    old_parallel = int(profile.get("stable_parallel") or profile.get("initial_parallel") or 4)
    max_parallel = int(profile.get("max_parallel") or old_parallel)
    step = int(profile.get("scale_step") or 2)
    new_parallel = old_parallel
    autoscale_reason = "hold"
    prior_rate = previous_metrics.get("net_review_per_worker_minute")
    saturation_candidate = selected >= min(old_parallel, int(plan.get("provider_available") or old_parallel))
    healthy = selected > 0 and saturation_candidate and successful >= max(1, int(selected * 0.8)) and error_rate <= 0.05 and rate_429 <= 0.01
    if desired.get("auto_scale", True):
        if error_rate >= 0.20 or rate_429 >= 0.05:
            new_parallel = max(2, old_parallel - step)
            autoscale_reason = "backoff_errors"
        elif healthy and net_review_added > 0:
            marginal_ok = prior_rate in (None, 0) or net_review_per_worker_minute >= float(prior_rate) * 0.65
            if marginal_ok and old_parallel < max_parallel:
                new_parallel = min(max_parallel, old_parallel + step)
                autoscale_reason = "healthy_canary_scale_up"
            else:
                autoscale_reason = "hold_marginal_throughput"
    avg_worker_minutes = mean([float(m.get("elapsed_seconds") or 0) / 60.0 for m in worker_metrics]) if worker_metrics else 0.0
    profile.update({
        "stable_parallel": new_parallel,
        "last_error_rate": round(error_rate, 4),
        "last_429_rate": round(rate_429, 4),
        "last_useful_per_minute": round(mean(useful_rates), 4) if useful_rates else 0,
        "last_cycle_useful_per_worker_minute": round(useful_per_worker_minute, 4),
        "canary_stage": int(profile.get("canary_stage") or 0) + (1 if new_parallel > old_parallel else 0),
        "last_autoscale_reason": autoscale_reason,
        "expected_worker_minutes": round(max(1.0, avg_worker_minutes), 3) if avg_worker_minutes else float(profile.get("expected_worker_minutes") or 20),
        "last_updated": now,
    })

    source_persist = persist_source_snapshot(plan_dir, plan, source_state)

    provider_capacity = load_json("state/provider_capacity.json", {"schema_version": 1})
    provider_capacity["updated_at"] = now
    provider_capacity.setdefault(args.provider, {}).update({
        "limit": plan.get("provider_limit"),
        "active_jobs": plan.get("active_jobs_observed"),
        "queued_jobs": plan.get("queued_jobs_observed"),
        "available_for_workers": plan.get("provider_available"),
        "selected_workers": selected,
    })
    estimated_cycle_credits = 0.0
    if args.provider == "circleci":
        month = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")
        cc_cfg = load_json("config/providers.json", {"providers": {}}).get("providers", {}).get("circleci", {})
        cc = provider_capacity.setdefault("circleci", {})
        if cc.get("month") != month:
            cc["month"] = month
            cc["estimated_credits_used"] = 0
        worker_rate = float(cc_cfg.get("worker_credits_per_minute") or 10)
        setup_rate = float(cc_cfg.get("setup_credits_per_minute") or 5)
        # Worker elapsed is measured; setup+aggregate use a conservative two-minute estimate each.
        estimated_cycle_credits = worker_minutes * worker_rate + (4.0 * setup_rate)
        cc["estimated_credits_used"] = round(float(cc.get("estimated_credits_used") or 0) + estimated_cycle_credits, 2)
        budget = float(cc_cfg.get("credit_budget_monthly") or cc_cfg.get("free_oss_credits_monthly") or 0)
        cc["estimated_credits_remaining"] = round(max(0.0, budget - cc["estimated_credits_used"]), 2)
        cc["credit_estimate_note"] = "Estimated from measured worker runtime plus conservative setup/aggregate allowance; CircleCI OSS credit balance is not exposed in the UI/API used here."

    metrics = {
        "schema_version": 1,
        "updated_at": now,
        "fleet_run_id": run_id,
        "provider": args.provider,
        "enabled": bool(plan.get("enabled")),
        "mode": plan.get("mode"),
        "selected_workers": selected,
        "worker_results_received": len(worker_metrics),
        "successful_workers": successful,
        "failed_or_missing_workers": failed,
        "error_rate": round(error_rate, 4),
        "rate_429": round(rate_429, 4),
        "raw_targets": sum(int(m.get("hub_targets") or 0) for m in worker_metrics),
        "records_materialized": sum(int(m.get("records_materialized") or 0) for m in worker_metrics),
        "review_candidates": useful_total,
        "new_entities": new_entities,
        "changed_entities": changed_entities,
        "duplicates_unchanged": duplicate_unchanged,
        "gpt_review_added": len(review_rows),
        "gpt_review_pending": pending.get("pending_records", 0),
        "useful_per_worker_minute": round(useful_per_worker_minute, 4),
        "net_review_per_worker_minute": round(net_review_per_worker_minute, 4),
        "source_parallel_before": old_parallel,
        "source_parallel_after": new_parallel,
        "autoscale_reason": autoscale_reason,
        "provider_limit": plan.get("provider_limit"),
        "active_jobs_observed": plan.get("active_jobs_observed"),
        "queued_jobs_observed": plan.get("queued_jobs_observed"),
        "available_backlog_before": plan.get("available_backlog"),
        "hub_snapshot_changed": source_persist.get("changed"),
        "hub_snapshot_sha256": source_persist.get("sha256"),
        "estimated_circleci_cycle_credits": round(estimated_cycle_credits, 2) if args.provider == "circleci" else 0,
    }

    gpt_summary = {
        "schema_version": 1,
        "status": "CYCLE_COMPLETE" if failed == 0 else "CYCLE_PARTIAL",
        "updated_at": now,
        "run_id": run_id,
        "provider": args.provider,
        "raw_discovered": metrics["raw_targets"],
        "new_unique": new_entities,
        "duplicates": duplicate_unchanged,
        "qualified_for_strict_review": useful_total,
        "owned_site_or_chain_rejects": sum(int(m.get("owned_site_or_chain_rejects") or 0) for m in worker_metrics),
        "uncertain": sum(int(m.get("uncertain") or 0) for m in worker_metrics),
        "gpt_review_added": len(review_rows),
        "gpt_review_pending": pending.get("pending_records", 0),
        "errors": failed,
        "provider_usage": {
            args.provider: {
                "limit": plan.get("provider_limit"),
                "selected_workers": selected,
                "active_observed_before": plan.get("active_jobs_observed"),
                "queued_observed_before": plan.get("queued_jobs_observed"),
            }
        },
        "coverage_delta": {"tasks_completed": successful, "tasks_failed_or_missing": failed},
        "next_backlog": max(0, int(plan.get("available_backlog") or 0) - successful),
        "autoscaling": {"before": old_parallel, "after": new_parallel, "reason": autoscale_reason},
        "review_batch": str(batch_path) if batch_path else None,
    }

    dump_json("state/gws_coverage.json", coverage)
    dump_json("state/gws_entity_index.json", entity_index)
    dump_json("state/gws_source_state.json", source_state)
    dump_json("state/gws_source_profiles.json", profiles)
    dump_json("state/provider_capacity.json", provider_capacity)
    dump_json("gpt/gws_pending_batches.json", pending)
    dump_json("gpt/gws_latest_summary.json", gpt_summary)
    dump_json("metrics/gws_latest.json", metrics)
    append_jsonl("metrics/gws_history.jsonl", [metrics])

    print(json.dumps({"metrics": metrics, "gpt_summary": gpt_summary}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
