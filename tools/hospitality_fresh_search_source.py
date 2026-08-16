#!/usr/bin/env python3
"""Fresh Hospitality discovery source using the existing SearchFabric.

This is a source adapter, not a second canonical pipeline. It searches a
deterministic long-tail catalog of premium/operator/PMS/direct-booking queries,
converts only non-OTA/non-social result domains into the existing V6 candidate
contract, and rejects canonical-known domains before expensive contact/live
checks.

The adapter itself never writes canonical state. Canonical mutation remains the
single-writer aggregate responsibility.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from hospitality_search_fabric import SearchFabric

ROOT = Path(__file__).resolve().parents[1]
CFG = ROOT / "config/hospitality_fresh_search_sources.json"
FIELDS = [
    "source","source_family","source_release","source_record_id","osm_type","osm_id",
    "country","region","name","category","brand","operator","website","domain",
    "public_email","email_domain","email_domain_match","public_phone","city","state",
    "street","confidence","operator_score","premium_score","fit_tier","source_url",
    "overture_id","notes","instagram","facebook"
]
MULTI = (
    "co.uk","org.uk","me.uk","ltd.uk","plc.uk","net.uk","com.au","net.au","org.au",
    "com.br","com.mx","co.nz","net.nz","org.nz","co.za","com.pt","com.es","com.tr",
    "co.jp","com.sg","com.hk","com.my"
)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def root_host(host: str) -> str:
    h = (host or "").lower().strip(".")
    if h.startswith("www."):
        h = h[4:]
    if not h:
        return ""
    for suffix in MULTI:
        if h.endswith("." + suffix):
            return ".".join(h.split(".")[-3:])
    parts = h.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else h


def domain_of(url: str) -> str:
    try:
        return root_host(urlparse(url).hostname or "")
    except Exception:
        return ""


def read_domains(path: str) -> set[str]:
    if not path or not Path(path).exists():
        return set()
    p = Path(path)
    opener = gzip.open if p.suffix == ".gz" else open
    with opener(p, "rt", encoding="utf-8") as f:
        return {root_host(x.strip()) for x in f if x.strip()}


def clean_title(title: str, domain: str) -> str:
    t = re.sub(r"\s+", " ", title or "").strip()
    parts = re.split(r"\s+[|–—]\s+|\s+-\s+", t)
    name = (parts[0] if parts else t).strip()
    bad = {"home","official site","book direct","vacation rentals","luxury villas"}
    if not name or name.lower() in bad:
        return domain
    return name[:180]


def normalize_market(item) -> dict:
    """Return only explicit geographic evidence from config.

    Legacy string markets are accepted for backwards compatibility, but no
    country/state is inferred from string token position.
    """
    if isinstance(item, dict):
        label = str(item.get("label") or item.get("market") or item.get("city") or "").strip()
        return {
            "id": str(item.get("id") or label).strip(),
            "label": label,
            "city": str(item.get("city") or "").strip(),
            "region": str(item.get("region") or item.get("state") or "").strip(),
            "state": str(item.get("state") or item.get("region") or "").strip(),
            "country": str(item.get("country") or "").strip(),
            "country_code": str(item.get("country_code") or "").strip().upper(),
        }
    label = str(item or "").strip()
    return {
        "id": label,
        "label": label,
        "city": "",
        "region": "",
        "state": "",
        "country": "",
        "country_code": "",
    }


def build_specs(cfg: dict) -> list[dict]:
    specs = []
    seen_queries = set()
    for market_item in cfg.get("markets") or []:
        market = normalize_market(market_item)
        if not market["label"]:
            continue
        for fam in cfg.get("query_families") or []:
            template = str(fam.get("template") or "").strip()
            if not template:
                continue
            try:
                query = template.format(
                    market=market["label"],
                    city=market["city"],
                    region=market["region"],
                    state=market["state"],
                    country=market["country"],
                    country_code=market["country_code"],
                ).strip()
            except Exception:
                continue
            key = re.sub(r"\s+", " ", query).strip().lower()
            if not key or key in seen_queries:
                continue
            seen_queries.add(key)
            specs.append({
                "family": str(fam.get("id") or "search"),
                "market": market,
                "family_spec": fam,
                "query": query,
            })
    return specs


def write_csv(path: Path, rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(CFG))
    ap.add_argument("--canonical-domains", default="")
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--max-queries", type=int, default=0)
    ap.add_argument("--cursor", type=int, default=0)
    args = ap.parse_args()

    cfg = load_json(Path(args.config))
    if not cfg.get("enabled"):
        raise SystemExit("fresh-search source disabled")
    policy = cfg.get("policy") or {}
    max_queries = args.max_queries or int(policy.get("max_queries_per_canary") or 30)
    max_results = int(policy.get("max_results_per_query") or 8)
    canonical = read_domains(args.canonical_domains)
    excluded = {root_host(x) for x in (cfg.get("excluded_domains") or []) if root_host(x)}

    catalog = build_specs(cfg)
    catalog_total = len(catalog)
    if catalog:
        offset = max(0, int(args.cursor)) % len(catalog)
        specs = (catalog[offset:] + catalog[:offset])[:max(0, max_queries)]
    else:
        specs = []

    fabric = SearchFabric({
        "timeout_seconds": float(policy.get("timeout_seconds") or 12),
        "max_results": max_results,
        "ddgs_enabled": True,
        "openserp_url": "",
        "searxng_url": "",
        "provider_delay_seconds": float(policy.get("provider_delay_seconds") or 0),
        "stop_after_first_successful_provider": bool(
            policy.get("stop_after_first_successful_provider", True)
        ),
    })

    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    observations = []
    events = []
    candidates: dict[str, dict] = {}
    raw_results = 0
    excluded_portal = 0
    canonical_known = 0
    duplicate_domain = 0
    t0 = time.time()

    for spec in specs:
        family = spec["family"]
        market = spec["market"]
        fam = spec["family_spec"]
        query = spec["query"]
        results, ev = fabric.search(family, query)
        events.extend([
            {
                **e,
                "market_id": market["id"],
                "market": market["label"],
                "query_family": family,
                "query": query,
            }
            for e in ev
        ])
        raw_results += len(results)
        for r in results:
            url = str(r.get("url") or "").strip()
            domain = domain_of(url)
            observations.append({
                **r,
                "market_id": market["id"],
                "market": market["label"],
                "country": market["country"],
                "query_family": family,
                "query": query,
                "domain": domain,
            })
            if not domain or domain in excluded or any(
                domain.endswith("." + x) for x in excluded
            ):
                excluded_portal += 1
                continue
            if domain in canonical:
                canonical_known += 1
                continue
            if domain in candidates:
                duplicate_domain += 1
                continue

            score_op = int(fam.get("operator_score") or 60)
            score_premium = int(fam.get("premium_score") or 60)
            rid = hashlib.sha256(
                f"{family}|{market['id']}|{domain}".encode()
            ).hexdigest()[:20]
            candidates[domain] = {
                "source": "SearchFabric fresh-search",
                "source_family": "search_fabric_fresh",
                "source_release": "fresh-v2",
                "source_record_id": f"fresh:{rid}",
                "osm_type": "",
                "osm_id": "",
                "country": market["country"],
                "region": market["region"],
                "name": clean_title(str(r.get("title") or ""), domain),
                "category": "hospitality_search_candidate",
                "brand": "",
                "operator": "",
                "website": url,
                "domain": domain,
                "public_email": "",
                "email_domain": "",
                "email_domain_match": "",
                "public_phone": "",
                "city": market["city"],
                "state": market["state"],
                "street": "",
                "confidence": "PUBLIC_SEARCH_RESULT_UNSEEN_DOMAIN",
                "operator_score": str(score_op),
                "premium_score": str(score_premium),
                "fit_tier": "A" if score_op >= 75 or score_premium >= 78 else "B",
                "source_url": url,
                "overture_id": "",
                "notes": (
                    f"Public search evidence; market_id={market['id']}; "
                    f"family={family}; provider={r.get('provider')}; "
                    f"rank={r.get('rank')}; query={query}"
                ),
                "instagram": "",
                "facebook": "",
            }

    rows = list(candidates.values())
    write_csv(out / "v6_recovery_candidates.csv", rows)
    with (out / "source_observations.jsonl").open("w", encoding="utf-8") as f:
        for row in observations:
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")

    summary = {
        "schema": "HOSPITALITY_FRESH_SEARCH_V2",
        "catalog_queries_total": catalog_total,
        "queries_planned": len(specs),
        "queries_with_provider_ok": sum(1 for e in events if e.get("status") == "OK"),
        "provider_events": len(events),
        "raw_search_results": raw_results,
        "excluded_portal_or_social": excluded_portal,
        "canonical_known_rejected_early": canonical_known,
        "duplicate_domain_results": duplicate_domain,
        "canonical_unseen_candidate_domains": len(rows),
        "canonical_snapshot_domains": len(canonical),
        "elapsed_seconds": round(time.time() - t0, 2),
        "cursor": int(args.cursor),
        "next_cursor": (int(args.cursor) + len(specs)) % catalog_total if catalog_total else 0,
        "canonical_mutation": False,
    }
    (out / "fresh_search_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    (out / "provider_events.json").write_text(
        json.dumps(events, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
