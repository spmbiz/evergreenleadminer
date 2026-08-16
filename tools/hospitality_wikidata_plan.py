#!/usr/bin/env python3
"""Bounded planner overlay for Wikidata hospitality discovery.

The master planner remains authoritative. This overlay injects at most the
configured Wikidata exploration cap, respecting canonical work-unit leases and
keeping the overall external-source ceiling bounded. It replaces geo first; if
all selected slots are already external sources, it replaces the lowest-priority
external source rather than growing fleet usage.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import fleet_runtime as fr

ROOT = Path(__file__).resolve().parents[1]
CFG = ROOT / "config/hospitality_wikidata_sources.json"


def _parse_ts(value):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def due_tasks(now: dt.datetime, coverage: dict, excluded: set[str]) -> list[dict]:
    cfg = fr.load_json(CFG, {})
    policy = cfg.get("policy") or {}
    if not cfg.get("enabled") or not cfg.get("production_enabled"):
        return []
    revisit = float(policy.get("revisit_hours") or 168)
    limit = max(1, int(policy.get("limit_per_shard") or 300))
    pages = max(1, int(policy.get("pages_per_country") or 3))
    release = now.strftime("%Y-%m-%d")
    tasks = []
    for country in cfg.get("countries") or []:
        qid = str(country.get("qid") or "")
        name = str(country.get("country") or "")
        code = str(country.get("code") or "")
        if not qid or not name:
            continue
        base_priority = int(country.get("priority") or 0)
        for page in range(pages):
            offset = page * limit
            shard = {
                "country": name,
                "region": f"WIKIDATA::{code or qid}::{offset}",
                "bbox": f"wikidata:{qid}:{offset}",
            }
            key = fr.shard_key(shard)
            if key in excluded:
                continue
            prior = coverage.get(key) or {}
            last = _parse_ts(prior.get("last_success"))
            age = 1e9 if not last else max(0.0, (now - last).total_seconds() / 3600.0)
            retryable = prior.get("status") in ("partial", "failed_retryable")
            if last and age < revisit and not retryable:
                continue
            tasks.append({
                "task_type": "wikidata_hospitality",
                "name": f"WIKIDATA::{code or qid}::{offset}",
                "country": name,
                "region": f"WIKIDATA::{code or qid}::{offset}",
                "bbox": f"wikidata:{qid}:{offset}",
                "release": release,
                "source_final_url": "https://query.wikidata.org/sparql",
                "spider": "",
                "extract_id": "",
                "search_cursor": offset,
                "search_queries": 0,
                "lane": "wikidata_hospitality",
                "lane_id": f"wikidata::{qid}::{offset}",
                "lane_phase": 1,
                "source_family": "wikidata_official_hospitality",
                "catalog_layer": "external-source",
                "tier": "SOURCE",
                "priority": 1200 + base_priority - page,
                "key": key,
                "max_rows": limit,
                "local_workers": int(policy.get("local_workers") or 32),
                "contact_workers": int(policy.get("contact_workers") or 24),
                "contact_timeout": 8,
                "contact_max_pages": 3,
                "contact_max_bytes": 700000,
                "revisit_hours": revisit,
                "source_age_hours": age,
                "source_changed": not last,
            })
    tasks.sort(key=lambda x: (int(x.get("priority") or 0), float(x.get("source_age_hours") or 0)), reverse=True)
    return tasks


def inject(plan: dict, excluded: set[str]) -> dict:
    cfg = fr.load_json(CFG, {})
    policy = cfg.get("policy") or {}
    cap = max(0, int(policy.get("source_slot_cap_per_cycle") or 0))
    capacity = max(0, int(plan.get("capacity") or 0))
    if cap <= 0 or capacity <= 0:
        return plan
    coverage = (fr.load_json(ROOT / "state/coverage.json", {}).get("shards") or {})
    due = due_tasks(dt.datetime.now(dt.timezone.utc), coverage, excluded)
    if not due:
        plan.setdefault("lane_backlog", {})["wikidata_hospitality"] = 0
        return plan

    selected = list(plan.get("include") or [])[:capacity]
    current = sum(str(x.get("task_type") or "") == "wikidata_hospitality" for x in selected)
    need = min(cap - current, len(due))
    if need <= 0:
        return plan

    occupied = {str(x.get("key") or "") for x in selected if x.get("key")}
    candidates = [x for x in due if str(x.get("key") or "") not in occupied]
    inserted = 0
    for task in candidates:
        if inserted >= need:
            break
        # Prefer replacing geo. Otherwise keep total source usage bounded by
        # replacing the lowest-priority existing external-source task.
        replace_idx = next((i for i in range(len(selected) - 1, -1, -1)
                            if str(selected[i].get("task_type") or "") == "geo"), None)
        if replace_idx is None and selected:
            replace_idx = min(
                range(len(selected)),
                key=lambda i: int(selected[i].get("priority") or 0),
            )
        if replace_idx is None:
            selected.append(task)
        else:
            selected[replace_idx] = task
        occupied.add(str(task.get("key") or ""))
        inserted += 1

    selected = selected[:capacity]
    for i, item in enumerate(selected):
        item["slot"] = i
    counts = {}
    for item in selected:
        lane = str(item.get("lane") or "unknown")
        counts[lane] = counts.get(lane, 0) + 1
    plan["include"] = selected
    plan["selected_lane_counts"] = counts
    plan.setdefault("lane_backlog", {})["wikidata_hospitality"] = len(due)
    plan["wikidata_backlog"] = len(due)
    plan["wikidata_inserted"] = inserted
    plan["useful_backlog"] = int(plan.get("useful_backlog") or 0) + len(due)
    return plan
