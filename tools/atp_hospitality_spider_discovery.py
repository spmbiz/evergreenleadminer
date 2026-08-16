#!/usr/bin/env python3
"""Read-only discovery of promising hospitality spiders in AllThePlaces.

Reads official ATP latest-run metadata/stats, filters likely hospitality spiders,
then samples only bounded per-spider GeoJSON streams. The output is evidence for
future canaries; it never mutates canonical state or production configuration.
"""
from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from urllib.parse import urlparse

import ijson
import requests

META = "https://data.alltheplaces.xyz/runs/latest.json"
OUT = "https://data.alltheplaces.xyz/runs/latest/output/{spider}.geojson"
TOKENS = (
    "hotel", "hotels", "resort", "resorts", "villa", "villas", "lodging",
    "hospitality", "holiday", "vacation", "hostel", "guesthouse", "motel",
    "inn_", "_inn", "apartments", "accommodation", "chalet",
)
EXCLUDE = ("hotel_chocolat", "pet_hotel", "animal_hotel")
MULTI = ("co.uk","com.au","co.nz","co.za","com.sg","com.hk","com.my")


def root_host(url: str) -> str:
    try:
        h = (urlparse(str(url or "")).hostname or "").lower().strip(".")
    except Exception:
        return ""
    if h.startswith("www."):
        h = h[4:]
    for suffix in MULTI:
        if h.endswith("." + suffix):
            return ".".join(h.split(".")[-3:])
    parts = h.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else h


def likely_hospitality(spider: str, filename: str) -> bool:
    text = f"{spider} {filename}".lower()
    if any(x in text for x in EXCLUDE):
        return False
    return any(x in text for x in TOKENS)


def sample_spider(session: requests.Session, spider: str, sample: int) -> dict:
    url = OUT.format(spider=spider)
    item = {"spider": spider, "url": url, "samples": 0, "hotel_like": 0,
            "public_email_samples": 0, "website_samples": 0, "distinct_domains": 0,
            "brands": [], "operators": [], "domains": []}
    domains, brands, operators = set(), set(), set()
    try:
        with session.get(url, stream=True, timeout=(8, 30), allow_redirects=True) as r:
            item["status_code"] = r.status_code
            r.raise_for_status()
            r.raw.decode_content = True
            for feature in ijson.items(r.raw, "features.item"):
                props = feature.get("properties") if isinstance(feature, dict) else {}
                if not isinstance(props, dict):
                    continue
                item["samples"] += 1
                tourism = str(props.get("tourism") or "").lower()
                category = str(props.get("category") or "").lower()
                if tourism in {"hotel", "resort", "motel", "hostel", "guest_house", "chalet"} or any(
                    x in category for x in ("hotel", "resort", "lodging", "accommodation")
                ):
                    item["hotel_like"] += 1
                email = str(props.get("email") or "").strip()
                if "@" in email:
                    item["public_email_samples"] += 1
                website = str(props.get("website") or "").strip()
                d = root_host(website)
                if d:
                    item["website_samples"] += 1
                    domains.add(d)
                brand = str(props.get("brand") or "").strip()
                operator = str(props.get("operator") or "").strip()
                if brand:
                    brands.add(brand)
                if operator:
                    operators.add(operator)
                if item["samples"] >= sample:
                    break
    except Exception as exc:
        item["error"] = f"{type(exc).__name__}: {exc}"
    item["distinct_domains"] = len(domains)
    item["domains"] = sorted(domains)[:12]
    item["brands"] = sorted(brands)[:12]
    item["operators"] = sorted(operators)[:12]
    n = max(1, item["samples"])
    distinct_ratio = item["distinct_domains"] / n
    hotel_ratio = item["hotel_like"] / n
    email_ratio = item["public_email_samples"] / n
    # Independent/member networks win; one-domain chain inventories are downweighted.
    item["sample_score"] = round(
        55 * distinct_ratio + 25 * email_ratio + 20 * hotel_ratio,
        2,
    )
    item["buyer_collapse_risk"] = bool(item["samples"] >= 3 and item["distinct_domains"] <= 1)
    return item


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-spiders", type=int, default=40)
    ap.add_argument("--sample", type=int, default=8)
    a = ap.parse_args()
    session = requests.Session()
    session.headers["User-Agent"] = "AIProdLeadHarvester/1.0 (+public-data-research)"
    meta = session.get(META, timeout=(8, 30)).json()
    stats_url = str(meta.get("stats_url") or "")
    if not stats_url:
        raise RuntimeError("ATP latest metadata missing stats_url")
    stats = session.get(stats_url, timeout=(8, 45)).json()
    results = list(stats.get("results") or [])
    candidates = []
    for row in results:
        spider = str(row.get("spider") or "")
        filename = str(row.get("filename") or "")
        if not spider or not likely_hospitality(spider, filename):
            continue
        features = int(row.get("features") or 0)
        errors = int(row.get("errors") or 0)
        if features <= 0:
            continue
        candidates.append({"spider": spider, "filename": filename, "features": features, "errors": errors})
    # Prefer useful-size, healthy spiders but keep bounded network load.
    candidates.sort(key=lambda x: (x["errors"] == 0, min(x["features"], 5000), x["features"]), reverse=True)
    candidates = candidates[: max(1, int(a.max_spiders))]
    sampled = []
    for row in candidates:
        item = sample_spider(session, row["spider"], max(3, int(a.sample)))
        item.update({"features": row["features"], "stats_errors": row["errors"], "filename": row["filename"]})
        sampled.append(item)
    sampled.sort(key=lambda x: (not x.get("buyer_collapse_risk"), float(x.get("sample_score") or 0), int(x.get("features") or 0)), reverse=True)
    payload = {
        "schema": "ATP_HOSPITALITY_SPIDER_DISCOVERY_V1",
        "run_id": meta.get("run_id"),
        "all_spiders": meta.get("spiders"),
        "stats_rows": len(results),
        "hospitality_name_candidates": len([r for r in results if likely_hospitality(str(r.get('spider') or ''), str(r.get('filename') or ''))]),
        "sampled_spiders": len(sampled),
        "recommended_canary_spiders": [
            x["spider"] for x in sampled
            if not x.get("error") and not x.get("buyer_collapse_risk")
            and int(x.get("hotel_like") or 0) >= 2
            and int(x.get("distinct_domains") or 0) >= 2
        ][:12],
        "results": sampled,
        "canonical_mutation": False,
    }
    p = Path(a.out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in payload.items() if k != "results"}, indent=2))


if __name__ == "__main__":
    main()
