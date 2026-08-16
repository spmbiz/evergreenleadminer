#!/usr/bin/env python3
"""Read-only Wikidata Hospitality source adapter.

Uses WDQS structured data to find hotels/resorts with an explicitly published
official website (P856). It does not scrape OTA pages and never mutates canonical
state. Canonical-known domains are rejected before contact enrichment.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

ENDPOINT = "https://query.wikidata.org/sparql"
UA = "AIProd-Hospitality-Wikidata/1.0 (public-business-research; contact via GitHub repo)"
MULTI = (
    "co.uk","org.uk","me.uk","ltd.uk","plc.uk","net.uk","com.au","net.au","org.au",
    "co.nz","net.nz","org.nz","com.pt","com.es","co.za","co.jp","com.sg","com.hk","com.my"
)
FIELDS = [
    "source","source_family","source_release","source_record_id","osm_type","osm_id",
    "country","region","name","category","brand","operator","website","domain","public_email",
    "email_domain","email_domain_match","public_phone","city","state","street","confidence",
    "operator_score","premium_score","fit_tier","source_url","overture_id","notes","instagram","facebook"
]


def root_host(value: str) -> str:
    h = (value or "").lower().strip(".")
    if h.startswith("www."):
        h = h[4:]
    if not h:
        return ""
    for suffix in MULTI:
        if h == suffix:
            return h
        if h.endswith("." + suffix):
            return ".".join(h.split(".")[-3:])
    p = h.split(".")
    return ".".join(p[-2:]) if len(p) >= 2 else h


def domain_of(url: str) -> str:
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https"):
            return ""
        return root_host(p.hostname or "")
    except Exception:
        return ""


def read_domains(path: str) -> set[str]:
    if not path or not Path(path).exists():
        return set()
    p = Path(path)
    opener = gzip.open if p.suffix == ".gz" else open
    with opener(p, "rt", encoding="utf-8") as f:
        return {root_host(x.strip()) for x in f if x.strip()}


def score_name(name: str, class_qid: str) -> tuple[int, int, str]:
    text = (name or "").lower()
    operator = 54
    premium = 64 if class_qid == "Q875157" else 54
    premium_terms = ("luxury", "boutique", "resort", "villa", "palace", "collection", "grand", "spa")
    if any(x in text for x in premium_terms):
        premium += 16
    if any(x in text for x in ("group", "collection", "hotels", "resorts")):
        operator += 14
    tier = "A" if operator >= 68 or premium >= 76 else "B"
    return min(operator, 100), min(premium, 100), tier


def build_query(country_qid: str, class_qids: list[str], limit: int, offset: int) -> str:
    values = " ".join(f"wd:{qid}" for qid in class_qids)
    return f"""
SELECT DISTINCT ?item ?itemLabel ?website ?class WHERE {{
  VALUES ?class {{ {values} }}
  ?item wdt:P31/wdt:P279* ?class ;
        wdt:P17 wd:{country_qid} ;
        wdt:P856 ?website .
  OPTIONAL {{ ?item rdfs:label ?itemLabel . FILTER(LANG(?itemLabel) = \"en\") }}
}}
LIMIT {int(limit)}
OFFSET {int(offset)}
""".strip()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--country-qid", required=True)
    ap.add_argument("--country", required=True)
    ap.add_argument("--country-code", default="")
    ap.add_argument("--classes", default="Q27686,Q875157")
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--timeout", type=float, default=30)
    ap.add_argument("--canonical-domains", default="")
    ap.add_argument("--outdir", required=True)
    a = ap.parse_args()

    out = Path(a.outdir)
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    canonical = read_domains(a.canonical_domains)
    class_qids = [x.strip() for x in a.classes.split(",") if re.fullmatch(r"Q\d+", x.strip())]
    query = build_query(a.country_qid, class_qids, max(1, a.limit), max(0, a.offset))

    headers = {
        "User-Agent": UA,
        "Accept": "application/sparql-results+json",
    }
    r = requests.post(
        ENDPOINT,
        data={"query": query, "format": "json"},
        headers=headers,
        timeout=(8, a.timeout),
    )
    r.raise_for_status()
    data = r.json()
    bindings = ((data.get("results") or {}).get("bindings") or [])

    rows_by_domain: dict[str, dict] = {}
    canonical_rejected = invalid_website = duplicate_domain = 0
    observations = []
    for b in bindings:
        item_url = str(((b.get("item") or {}).get("value")) or "")
        item_qid = item_url.rsplit("/", 1)[-1] if item_url else ""
        name = str(((b.get("itemLabel") or {}).get("value")) or item_qid)
        website = str(((b.get("website") or {}).get("value")) or "").strip()
        class_url = str(((b.get("class") or {}).get("value")) or "")
        class_qid = class_url.rsplit("/", 1)[-1] if class_url else ""
        domain = domain_of(website)
        observations.append({
            "item_qid": item_qid,
            "name": name,
            "website": website,
            "domain": domain,
            "class_qid": class_qid,
            "country_qid": a.country_qid,
            "country": a.country,
        })
        if not domain:
            invalid_website += 1
            continue
        if domain in canonical:
            canonical_rejected += 1
            continue
        if domain in rows_by_domain:
            duplicate_domain += 1
            continue
        op, premium, tier = score_name(name, class_qid)
        rows_by_domain[domain] = {
            "source": "Wikidata WDQS official website",
            "source_family": "wikidata_official_hospitality",
            "source_release": time.strftime("%Y-%m-%d", time.gmtime()),
            "source_record_id": item_qid,
            "osm_type": "",
            "osm_id": "",
            "country": a.country,
            "region": "",
            "name": name[:180],
            "category": "resort" if class_qid == "Q875157" else "hotel",
            "brand": "",
            "operator": "",
            "website": website,
            "domain": domain,
            "public_email": "",
            "email_domain": "",
            "email_domain_match": "",
            "public_phone": "",
            "city": "",
            "state": "",
            "street": "",
            "confidence": "WIKIDATA_EXPLICIT_OFFICIAL_WEBSITE",
            "operator_score": str(op),
            "premium_score": str(premium),
            "fit_tier": tier,
            "source_url": item_url,
            "overture_id": "",
            "notes": f"Wikidata structured evidence; country={a.country_qid}; class={class_qid}; official website property P856.",
            "instagram": "",
            "facebook": "",
        }

    rows = list(rows_by_domain.values())
    with (out / "v6_recovery_candidates.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    with (out / "source_observations.jsonl").open("w", encoding="utf-8") as f:
        for row in observations:
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")

    summary = {
        "schema": "HOSPITALITY_WIKIDATA_SOURCE_V1",
        "country_qid": a.country_qid,
        "country": a.country,
        "classes": class_qids,
        "limit": a.limit,
        "offset": a.offset,
        "raw_bindings": len(bindings),
        "invalid_website": invalid_website,
        "canonical_known_rejected_early": canonical_rejected,
        "duplicate_domain_results": duplicate_domain,
        "canonical_unseen_candidate_domains": len(rows),
        "canonical_snapshot_domains": len(canonical),
        "elapsed_seconds": round(time.time() - t0, 2),
        "canonical_mutation": False,
    }
    (out / "wikidata_source_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
