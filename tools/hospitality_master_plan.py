#!/usr/bin/env python3
"""Unified planner: Overture world backlog + bounded external-source exploration.

Overture remains the coverage backbone. Approved external sources consume only a
small capped share of a cycle, and only when their upstream version changed,
they have never completed, a retry is due, or their revisit interval elapsed.
Any source-version lookup failure degrades to geographic work; it never blocks
fleet planning.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

import fleet_runtime as fr

ROOT = Path(__file__).resolve().parents[1]
ATP_CFG = ROOT / "config/atp_hospitality_spiders.json"
OSM_CFG = ROOT / "config/osm_geofabrik_sources.json"
FRESH_CFG = ROOT / "config/hospitality_fresh_search_sources.json"
UA = "AIProdLeadHarvester/1.0 (+public-business-research)"


def parse_ts(value):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def due(prior, release, revisit, now):
    last = parse_ts(prior.get("last_success"))
    age = 1e9 if not last else max(0.0, (now - last).total_seconds() / 3600.0)
    changed = str(prior.get("release") or "") != str(release or "")
    retryable = prior.get("status") in ("partial", "failed_retryable")
    return changed or not last or age >= revisit or retryable, age, changed


def atp_current_run(spider: str) -> tuple[str, str]:
    url = f"https://data.alltheplaces.xyz/runs/latest/output/{spider}.geojson"
    headers = {"User-Agent": UA}
    last_error = None
    for method in ("HEAD", "GET"):
        req = urllib.request.Request(url, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=12) as response:
                final = response.geturl()
                match = re.search(r"/runs/([^/]+)/output/", final)
                if match:
                    return match.group(1), final
                last_error = RuntimeError(f"could not parse ATP run from {final}")
        except Exception as e:
            last_error = e
    raise RuntimeError(f"ATP release resolve failed for {spider}: {type(last_error).__name__}: {last_error}")


def http_version(url: str) -> tuple[str, dict]:
    req = urllib.request.Request(url, headers={"User-Agent": UA}, method="HEAD")
    with urllib.request.urlopen(req, timeout=15) as response:
        headers = {k.lower(): v for k, v in response.headers.items()}
        etag = headers.get("etag", "").strip().removeprefix("W/").strip().strip('"')
        modified = headers.get("last-modified", "").strip()
        length = headers.get("content-length", "").strip()
        version = "|".join(x for x in (etag, modified, length) if x)
        if not version:
            raise RuntimeError(f"no stable HTTP version headers for {url}")
        return version, {
            "etag": etag,
            "last_modified": modified,
            "content_length": length,
            "final_url": response.geturl(),
        }


def atp_tasks(now, coverage):
    cfg = fr.load_json(ATP_CFG, {})
    if not (cfg.get("policy") or {}).get("production_enabled"):
        return [], [], 0
    tasks, errors = [], []
    cap = int((cfg.get("policy") or {}).get("source_slot_cap_per_cycle") or 0)
    for item in cfg.get("spiders") or []:
        if not item.get("production_enabled"):
            continue
        spider = str(item.get("spider") or "")
        if not spider:
            continue
        try:
            release, final_url = atp_current_run(spider)
        except Exception as e:
            errors.append({"source": "alltheplaces", "spider": spider, "error": f"{type(e).__name__}: {e}"})
            continue
        shard = {"country": "GLOBAL", "region": f"ATP::{spider}", "bbox": f"atp:{spider}"}
        key = fr.shard_key(shard)
        revisit = float(item.get("revisit_hours") or 168)
        is_due, age, changed = due(coverage.get(key) or {}, release, revisit, now)
        if not is_due:
            continue
        tasks.append({
            "task_type": "atp_spider", "name": f"ATP::{spider}", "country": "GLOBAL", "region": f"ATP::{spider}", "bbox": f"atp:{spider}",
            "release": release, "source_final_url": final_url, "spider": spider, "extract_id": "", "search_cursor": 0, "search_queries": 0,
            "lane": "atp_directory_contact" if item.get("mode") == "trusted_directory_contact" else "atp_first_party",
            "lane_id": f"atp::{spider}", "lane_phase": 2, "source_family": "alltheplaces", "catalog_layer": "external-source", "tier": "SOURCE",
            "priority": int(item.get("priority") or 0), "key": key, "max_rows": 0, "local_workers": 32, "contact_workers": 0, "contact_timeout": 8,
            "contact_max_pages": 0, "contact_max_bytes": 0, "revisit_hours": revisit, "source_age_hours": age, "source_changed": changed,
        })
    return tasks, errors, cap


def osm_tasks(now, coverage):
    cfg = fr.load_json(OSM_CFG, {})
    if not (cfg.get("policy") or {}).get("production_enabled"):
        return [], [], 0
    tasks, errors = [], []
    cap = int((cfg.get("policy") or {}).get("source_slot_cap_per_cycle") or 0)
    for item in cfg.get("extracts") or []:
        if not item.get("production_enabled"):
            continue
        extract_id = str(item.get("extract_id") or "")
        pbf_url = str(item.get("pbf_url") or "")
        if not extract_id or not pbf_url:
            continue
        try:
            release, headers = http_version(pbf_url)
        except Exception as e:
            errors.append({"source": "openstreetmap_geofabrik", "extract_id": extract_id, "error": f"{type(e).__name__}: {e}"})
            continue
        shard = {"country": str(item.get("country") or ""), "region": f"OSM::{extract_id}", "bbox": f"osm:{extract_id}"}
        key = fr.shard_key(shard)
        revisit = float(item.get("revisit_hours") or 168)
        is_due, age, changed = due(coverage.get(key) or {}, release, revisit, now)
        if not is_due:
            continue
        tasks.append({
            "task_type": "osm_geofabrik", "name": f"OSM::{extract_id}", "country": str(item.get("country") or ""), "region": f"OSM::{extract_id}", "bbox": f"osm:{extract_id}",
            "release": release, "source_final_url": headers.get("final_url") or pbf_url, "spider": "", "extract_id": extract_id, "search_cursor": 0, "search_queries": 0,
            "lane": "osm_geofabrik", "lane_id": f"osm::{extract_id}", "lane_phase": 2, "source_family": "openstreetmap_geofabrik", "catalog_layer": "external-source", "tier": "SOURCE",
            "priority": int(item.get("priority") or 0), "key": key, "max_rows": 0, "local_workers": 32, "contact_workers": 16, "contact_timeout": 8,
            "contact_max_pages": 3, "contact_max_bytes": 700000, "revisit_hours": revisit, "source_age_hours": age, "source_changed": changed,
        })
    return tasks, errors, cap


def fresh_search_tasks(now, coverage):
    cfg = fr.load_json(FRESH_CFG, {})
    if not cfg.get("production_enabled"):
        return [], [], 0
    policy = cfg.get("policy") or {}
    cap = int(policy.get("source_slot_cap_per_cycle") or 0)
    shard_queries = max(1, int(policy.get("production_shard_query_count") or 30))
    revisit = float(policy.get("revisit_hours") or 24)
    total_queries = len(cfg.get("markets") or []) * len(cfg.get("query_families") or [])
    if total_queries <= 0:
        return [], [{"source": "fresh_search", "error": "empty query catalog"}], cap
    release = now.strftime("%Y-%m-%d")
    tasks = []
    for cursor in range(0, total_queries, shard_queries):
        count = min(shard_queries, total_queries - cursor)
        shard = {"country": "MULTI", "region": f"FRESH_SEARCH::{cursor}", "bbox": f"fresh-search:{cursor}"}
        key = fr.shard_key(shard)
        is_due, age, changed = due(coverage.get(key) or {}, release, revisit, now)
        if not is_due:
            continue
        tasks.append({
            "task_type": "fresh_search", "name": f"FRESH_SEARCH::{cursor}", "country": "MULTI", "region": f"FRESH_SEARCH::{cursor}", "bbox": f"fresh-search:{cursor}",
            "release": release, "source_final_url": "", "spider": "", "extract_id": "", "search_cursor": cursor, "search_queries": count,
            "lane": "fresh_search", "lane_id": f"fresh-search::{cursor}", "lane_phase": 1, "source_family": "search_fabric_fresh", "catalog_layer": "external-source", "tier": "SOURCE",
            "priority": 1000 - cursor, "key": key, "max_rows": count, "local_workers": 32, "contact_workers": 24, "contact_timeout": 8,
            "contact_max_pages": 3, "contact_max_bytes": 700000, "revisit_hours": revisit, "source_age_hours": age, "source_changed": changed,
        })
    return tasks, [], cap


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=("github", "circleci"), default="github")
    ap.add_argument("--capacity", type=int, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--ignore-coverage", action="store_true")
    ap.add_argument("--force-lane", default="")
    a = ap.parse_args()

    now = dt.datetime.now(dt.timezone.utc)
    coverage = (fr.load_json(ROOT / "state/coverage.json", {}).get("shards") or {})
    atp, atp_errors, atp_cap = atp_tasks(now, coverage)
    osm, osm_errors, osm_cap = osm_tasks(now, coverage)
    fresh, fresh_errors, fresh_cap = fresh_search_tasks(now, coverage)

    # Enforce each source's own slot ceiling before applying the global source
    # ceiling. This prevents one high-priority source from consuming the budget
    # reserved for the other additive source rails.
    source_due = (
        fresh[:max(0, fresh_cap)]
        + atp[:max(0, atp_cap)]
        + osm[:max(0, osm_cap)]
    )
    source_due.sort(
        key=lambda x: (
            bool(x.get("source_changed")),
            int(x.get("priority") or 0),
            float(x.get("source_age_hours") or 0),
            x["name"],
        ),
        reverse=True,
    )
    total_source_cap = min(4, max(0, atp_cap) + max(0, osm_cap) + max(0, fresh_cap))

    force = (a.force_lane or "").lower().strip()
    if force in ("atp", "alltheplaces", "atp_directory_contact"):
        source_due = [x for x in source_due if x.get("task_type") == "atp_spider"]
        total_source_cap = min(int(a.capacity), max(0, atp_cap))
    elif force in ("osm", "geofabrik", "osm_geofabrik"):
        source_due = [x for x in source_due if x.get("task_type") == "osm_geofabrik"]
        total_source_cap = min(int(a.capacity), max(0, osm_cap))
    elif force in ("fresh", "fresh_search", "search_fabric_fresh"):
        source_due = [x for x in source_due if x.get("task_type") == "fresh_search"]
        total_source_cap = min(int(a.capacity), max(0, fresh_cap))

    source_n = min(max(0, int(a.capacity)), max(0, total_source_cap), len(source_due))
    selected_sources = source_due[:source_n]
    geo_capacity = max(0, int(a.capacity) - source_n)
    source_only_force = force in (
        "atp", "alltheplaces", "atp_directory_contact",
        "osm", "geofabrik", "osm_geofabrik",
        "fresh", "fresh_search", "search_fabric_fresh",
    )
    if source_only_force:
        geo_capacity = 0

    tmp = Path(a.out).with_suffix(".geo.json")
    cmd = [sys.executable, "tools/hospitality_multilane_plan.py", "--provider", a.provider, "--capacity", str(geo_capacity), "--out", str(tmp)]
    if a.ignore_coverage:
        cmd.append("--ignore-coverage")
    if a.force_lane and not source_only_force:
        cmd += ["--force-lane", a.force_lane]
    subprocess.run(cmd, cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
    geo = fr.load_json(tmp, {})
    try:
        tmp.unlink()
    except Exception:
        pass
    geos = geo.get("include") or []
    for item in geos:
        item["task_type"] = "geo"
        item["spider"] = ""
        item["extract_id"] = ""
        item["search_cursor"] = 0
        item["search_queries"] = 0

    selected = selected_sources + geos
    for i, item in enumerate(selected):
        item["slot"] = i
    lane_backlog = dict(geo.get("lane_backlog") or {})
    lane_backlog["atp_directory_contact"] = sum(x.get("task_type") == "atp_spider" for x in atp)
    lane_backlog["osm_geofabrik"] = len(osm)
    lane_backlog["fresh_search"] = len(fresh)
    selected_lane_counts = {}
    for item in selected:
        lane = str(item.get("lane") or "unknown")
        selected_lane_counts[lane] = selected_lane_counts.get(lane, 0) + 1

    external_count = len(atp) + len(osm) + len(fresh)
    payload = dict(geo)
    payload.update({
        "capacity": int(a.capacity),
        "geo_capacity": geo_capacity,
        "source_slot_cap": total_source_cap,
        "external_source_backlog": external_count,
        "source_errors": atp_errors + osm_errors + fresh_errors,
        "useful_backlog": int(geo.get("useful_backlog") or 0) + external_count,
        "lane_backlog": lane_backlog,
        "selected_lane_counts": selected_lane_counts,
        "include": selected,
    })
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in payload.items() if k != "include"}, indent=2))


if __name__ == "__main__":
    main()
