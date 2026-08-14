#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import time
import unicodedata
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlparse

import duckdb

DEFAULT_RELEASE = "2026-06-17.0"
PLATFORM_DOMAINS = (
    "facebook.com", "instagram.com", "tiktok.com", "linkedin.com", "youtube.com",
    "treatwell.be", "mytreatwell.be", "planity.com", "fresha.com", "salonkee.be",
    "pagesdor.be", "goudengids.be", "bizique.be", "cylex-belgie.be", "opendi.be",
    "bolid.be", "garagebelgique.com", "selfcity.be", "heures.be", "openingsuren.vlaanderen",
    "tripadvisor.", "yelp.", "google.", "maps.apple.", "waze.com", "ubereats.com",
    "deliveroo.", "takeaway.com", "booking.com", "nearcut.", "brusselslife.be",
)
CHAIN_WORDS = (
    "carrefour", "delhaize", "lidl", "aldi", "action", "kruidvat", "ici paris", "di beauty",
    "basic-fit", "orange", "proximus", "base shop", "telenet", "mediamarkt", "quick",
    "mcdonald", "burger king", "starbucks", "pizza hut", "domino", "panos", "exki",
    "fintro", "bnp paribas", "belfius", "ing", "kbc", "crelan",
)


def txt(v):
    if v is None:
        return ""
    return re.sub(r"\s+", " ", str(v)).strip()


def norm(s):
    s = unicodedata.normalize("NFKD", txt(s)).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def tokens(s):
    return {
        x for x in norm(s).split()
        if len(x) > 1 and x not in {"the", "de", "la", "le", "les", "du", "des", "and", "et", "sa", "sprl", "srl", "bv", "nv"}
    }


def sim(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    seq = SequenceMatcher(None, a, b).ratio()
    ta, tb = tokens(a), tokens(b)
    jac = len(ta & tb) / max(1, len(ta | tb))
    contains = 1.0 if (a in b or b in a) and min(len(a), len(b)) >= 4 else 0.0
    return max(seq, 0.65 * seq + 0.35 * jac, 0.72 * contains + 0.28 * jac)


def hav(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1, math.sqrt(a)))


def first(v):
    if isinstance(v, (list, tuple)):
        return txt(v[0]) if v else ""
    return txt(v)


def host(u):
    try:
        h = (urlparse(txt(u)).hostname or "").lower().strip(".")
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""


def is_platform(h):
    h = (h or "").lower()
    return any(x in h for x in PLATFORM_DOMAINS)


def owned_site(websites):
    if not websites:
        return ""
    vals = websites if isinstance(websites, (list, tuple)) else [websites]
    for u in vals:
        h = host(u)
        if h and not is_platform(h):
            return txt(u)
    return ""


def brand_name(v):
    if not isinstance(v, dict):
        return ""
    n = v.get("names")
    if isinstance(n, dict):
        return txt(n.get("primary"))
    return txt(v.get("name"))


def geo(r):
    g = r.get("geo_point_2d") or {}
    return float(g.get("lat")), float(g.get("lon"))


def business_name(r):
    return txt(r.get("name_en") or r.get("name_fr") or r.get("name_nl"))


def business_type(r):
    return txt(r.get("type_en") or r.get("type_fr") or r.get("type_nl"))


def business_category(r):
    return txt(r.get("category_en") or r.get("category_fr") or r.get("category_nl"))


def business_address(r):
    return txt(r.get("address_fr") or r.get("address_nl") or r.get("address_en"))


def postal_code(r):
    return txt(r.get("postalcode") or r.get("postal_code") or r.get("zip"))


def hub_id(r):
    return txt(r.get("objectid") or r.get("recordid") or r.get("id"))


def target_row(r: dict, postcodes: set[str], keywords: tuple[str, ...]) -> bool:
    n = norm(business_name(r))
    typ = norm(business_type(r))
    cat = norm(business_category(r))
    pc = postal_code(r)
    if not n or n == "-" or "empty commercial cell" in typ:
        return False
    if postcodes and pc not in postcodes:
        return False
    if any(c in n for c in CHAIN_WORDS):
        return False
    hay = f"{typ} {cat}"
    return any(norm(k) in hay for k in keywords if norm(k))


def record_key(row: dict) -> str:
    source_id = txt(row.get("hub_objectid"))
    if source_id:
        return f"hub:{source_id}"
    raw = "|".join([norm(row.get("hub_name")), norm(row.get("hub_address")), txt(row.get("hub_postalcode"))])
    return "gws:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def query_overture(targets: list[dict], release: str, threads: int) -> tuple[list[dict], list[float]]:
    if not targets:
        return [], []
    lats, lons = [], []
    for r in targets:
        try:
            lat, lon = geo(r)
            lats.append(lat); lons.append(lon)
        except Exception:
            pass
    if not lats:
        return [], []
    # Tight target-only bbox plus ~1.5 km padding, unlike the old whole-Brussels resolver.
    minlat, maxlat = min(lats) - 0.015, max(lats) + 0.015
    minlon, maxlon = min(lons) - 0.020, max(lons) + 0.020
    con = duckdb.connect()
    con.execute(f"PRAGMA threads={max(1, int(threads))}")
    con.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2';")
    path = f"s3://overturemaps-us-west-2/release/{release}/theme=places/type=place/*"
    q = f"""
      SELECT id, names.primary AS name, basic_category, categories.primary AS category_primary,
             websites, socials, emails, phones, brand, addresses, confidence, operating_status,
             (bbox.xmin+bbox.xmax)/2.0 AS longitude,
             (bbox.ymin+bbox.ymax)/2.0 AS latitude
      FROM read_parquet('{path}', hive_partitioning=1)
      WHERE bbox.xmax >= {minlon} AND bbox.xmin <= {maxlon}
        AND bbox.ymax >= {minlat} AND bbox.ymin <= {maxlat}
        AND (operating_status IS NULL OR operating_status='open')
        AND names.primary IS NOT NULL
    """
    cur = con.execute(q)
    cols = [d[0] for d in cur.description]
    places = [dict(zip(cols, x)) for x in cur.fetchall()]
    return places, [minlon, minlat, maxlon, maxlat]


def resolve(targets: list[dict], places: list[dict], max_serious: int) -> list[dict]:
    scale = 1000
    grid: dict[tuple[int, int], list[dict]] = defaultdict(list)
    for p in places:
        try:
            key = (int(float(p["latitude"]) * scale), int(float(p["longitude"]) * scale))
            grid[key].append(p)
        except Exception:
            continue

    rows = []
    for h in targets:
        try:
            lat, lon = geo(h)
        except Exception:
            continue
        name = business_name(h)
        key = (int(lat * scale), int(lon * scale))
        best = None
        for di in range(-2, 3):
            for dj in range(-2, 3):
                for p in grid.get((key[0] + di, key[1] + dj), []):
                    d = hav(lat, lon, float(p["latitude"]), float(p["longitude"]))
                    if d > 120:
                        continue
                    ns = sim(name, p.get("name"))
                    score = ns * 100 - min(d, 100) * 0.18 + float(p.get("confidence") or 0) * 8
                    if best is None or score > best[0]:
                        best = (score, d, ns, p)
        resolved = False
        p = None
        dist = None
        ns = 0.0
        if best:
            _, dist, ns, p = best
            resolved = ((dist <= 25 and ns >= 0.72) or (dist <= 12 and ns >= 0.55) or (dist <= 50 and ns >= 0.90))

        site = owned_site(p.get("websites")) if resolved else ""
        brand = brand_name(p.get("brand")) if resolved else ""
        chain = bool(brand and any(c in norm(brand) for c in CHAIN_WORDS))
        if resolved and (site or chain):
            outcome = "REJECT"
            reason = "OWNED_SITE_FOUND" if site else "CHAIN_BRAND"
            needs_review = False
        elif resolved:
            # Important: Overture absence is NOT enough for strict VERIFIED_NO_WEBSITE.
            outcome = "REVIEW"
            reason = "CURRENT_ENTITY_RESOLVED_NO_OWNED_SITE_IN_OVERTURE"
            needs_review = True
        else:
            outcome = "UNCERTAIN"
            reason = "NO_EXACT_CURRENT_PLACE_MATCH"
            needs_review = True

        row = {
            "hub_objectid": hub_id(h),
            "hub_name": name,
            "hub_type": business_type(h),
            "hub_category": business_category(h),
            "hub_address": business_address(h),
            "hub_postalcode": postal_code(h),
            "hub_google_maps": txt(h.get("google_maps")),
            "hub_lat": lat,
            "hub_lon": lon,
            "overture_id": txt(p.get("id")) if resolved else "",
            "overture_name": txt(p.get("name")) if resolved else "",
            "distance_m": round(float(dist), 1) if resolved and dist is not None else "",
            "name_similarity": round(ns, 3) if resolved else "",
            "overture_confidence": txt(p.get("confidence")) if resolved else "",
            "overture_category": txt(p.get("category_primary") or p.get("basic_category")) if resolved else "",
            "overture_phone": first(p.get("phones")) if resolved else "",
            "overture_email": first(p.get("emails")) if resolved else "",
            "overture_websites": json.dumps(p.get("websites"), ensure_ascii=False, default=str) if resolved and p.get("websites") else "",
            "owned_website": site,
            "overture_socials": json.dumps(p.get("socials"), ensure_ascii=False, default=str) if resolved and p.get("socials") else "",
            "overture_brand": brand,
            "outcome": outcome,
            "reason": reason,
            "needs_gpt_review": needs_review,
        }
        row["record_key"] = record_key(row)
        rows.append(row)

    rank = {"REVIEW": 0, "UNCERTAIN": 1, "REJECT": 2}
    rows.sort(
        key=lambda r: (
            rank.get(r["outcome"], 9),
            -float(r["overture_confidence"] or 0),
            float(r["distance_m"] or 999),
            -float(r["name_similarity"] or 0),
        )
    )
    # Preserve deterministic rejects too, but cap review-heavy output per task.
    serious = []
    review_count = 0
    for r in rows:
        if r["outcome"] in {"REVIEW", "UNCERTAIN"}:
            if review_count >= max_serious:
                continue
            review_count += 1
        serious.append(r)
    return serious


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task-id")
    ap.add_argument("--workload-id")
    ap.add_argument("--territory")
    ap.add_argument("--postal-codes-json")
    ap.add_argument("--family")
    ap.add_argument("--keywords-json")
    ap.add_argument("--max-serious", type=int, default=300)
    ap.add_argument("--lease-id")
    ap.add_argument("--lease-expires-at")
    ap.add_argument("--plan-json")
    ap.add_argument("--task-index", type=int)
    ap.add_argument("--hub-snapshot", default="results/fleet_plan/hub_brussels_current.jsonl")
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--release", default=DEFAULT_RELEASE)
    ap.add_argument("--threads", type=int, default=4)
    args = ap.parse_args()

    if args.plan_json is not None:
        if args.task_index is None:
            ap.error("--task-index is required with --plan-json")
        plan = json.loads(Path(args.plan_json).read_text(encoding="utf-8"))
        tasks = plan.get("tasks", [])
        if args.task_index < 0 or args.task_index >= len(tasks):
            print(json.dumps({"status":"noop","reason":"task_index_out_of_range","task_index":args.task_index,"selected_count":len(tasks)}))
            return 0
        task = tasks[args.task_index]
        args.task_id = task["task_id"]
        args.workload_id = task["workload_id"]
        args.territory = task["territory"]
        args.postal_codes_json = task["postal_codes_json"]
        args.family = task["family"]
        args.keywords_json = task["keywords_json"]
        args.max_serious = int(task.get("max_serious", args.max_serious))
        args.lease_id = task["lease_id"]
        args.lease_expires_at = task["lease_expires_at"]
    required = ["task_id","workload_id","territory","postal_codes_json","family","keywords_json","lease_id","lease_expires_at"]
    missing = [name for name in required if not getattr(args, name)]
    if missing:
        ap.error("missing task fields: " + ", ".join(missing))

    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    status = "completed"
    error = ""
    targets: list[dict] = []
    places: list[dict] = []
    records: list[dict] = []
    bbox: list[float] = []
    try:
        postcodes = set(json.loads(args.postal_codes_json))
        keywords = tuple(json.loads(args.keywords_json))
        with Path(args.hub_snapshot).open("r", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                row = json.loads(line)
                if target_row(row, postcodes, keywords):
                    targets.append(row)
        places, bbox = query_overture(targets, args.release, args.threads)
        records = resolve(targets, places, args.max_serious)
        for r in records:
            r.update({
                "task_id": args.task_id,
                "workload_id": args.workload_id,
                "territory": args.territory,
                "family": args.family,
                "lease_id": args.lease_id,
            })
        with (out / "records.jsonl").open("w", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
        if records:
            fields = list(records[0].keys())
            with (out / "records.csv").open("w", encoding="utf-8", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=fields)
                w.writeheader(); w.writerows(records)
    except Exception as exc:
        status = "failed_retryable"
        error = f"{type(exc).__name__}: {exc}"

    elapsed = max(0.001, time.time() - t0)
    outcomes = Counter(r.get("outcome") for r in records)
    useful = int(outcomes.get("REVIEW", 0))
    metrics = {
        "schema_version": 1,
        "task_id": args.task_id,
        "workload_id": args.workload_id,
        "territory": args.territory,
        "family": args.family,
        "lease_id": args.lease_id,
        "status": status,
        "error": error,
        "release": args.release,
        "hub_targets": len(targets),
        "overture_places_in_bbox": len(places),
        "records_materialized": len(records),
        "review_candidates": useful,
        "uncertain": int(outcomes.get("UNCERTAIN", 0)),
        "owned_site_or_chain_rejects": int(outcomes.get("REJECT", 0)),
        "elapsed_seconds": round(elapsed, 3),
        "useful_per_minute": round(useful / elapsed * 60.0, 4),
        "bbox": bbox,
        "lease_expires_at": args.lease_expires_at,
    }
    (out / "metrics.json").write_text(json.dumps(metrics, indent=2, ensure_ascii=False), encoding="utf-8")
    checkpoint = {
        "task_id": args.task_id,
        "lease_id": args.lease_id,
        "status": status,
        "finished_at_epoch": int(time.time()),
        "error": error,
    }
    (out / "checkpoint.json").write_text(json.dumps(checkpoint, indent=2), encoding="utf-8")
    print(json.dumps(metrics, indent=2, ensure_ascii=False))
    return 0 if status == "completed" else 75


if __name__ == "__main__":
    raise SystemExit(main())
