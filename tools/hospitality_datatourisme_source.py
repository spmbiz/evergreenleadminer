#!/usr/bin/env python3
"""Read-only DATAtourisme hospitality discovery adapter.

Resolves the current stable resource from the official data.gouv.fr dataset API,
streams the CSV, keeps explicit accommodation-category POIs with a public website,
and rejects canonical-known domains before expensive contact enrichment.

No canonical mutation happens here.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
import tempfile
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]
CFG = ROOT / "config/hospitality_datatourisme_sources.json"
URL_RE = re.compile(r"https?://[^\s<>#|\"]+", re.I)
MULTI = (
    "co.uk","org.uk","me.uk","ltd.uk","plc.uk","net.uk","com.au","net.au","org.au",
    "co.nz","net.nz","org.nz","com.pt","com.es","co.za","co.jp","com.sg","com.hk","com.my"
)
PORTAL_DOMAINS = {
    "airbnb.com","booking.com","vrbo.com","expedia.com","tripadvisor.com","hotels.com","agoda.com",
    "facebook.com","instagram.com","youtube.com","tiktok.com","pinterest.com","google.com",
}
FIELDS = [
    "source","source_family","source_release","source_record_id","osm_type","osm_id",
    "country","region","name","category","brand","operator","website","domain","public_email",
    "email_domain","email_domain_match","public_phone","city","state","street","confidence",
    "operator_score","premium_score","fit_tier","source_url","overture_id","notes","instagram","facebook"
]


def norm_key(value: str) -> str:
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(c for c in value if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", "", value)


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
        return root_host(p.hostname or "") if p.scheme in ("http", "https") else ""
    except Exception:
        return ""


def read_domains(path: str) -> set[str]:
    p = Path(path) if path else Path("__missing__")
    if not p.exists():
        return set()
    opener = gzip.open if p.suffix == ".gz" else open
    with opener(p, "rt", encoding="utf-8") as f:
        return {root_host(x.strip()) for x in f if x.strip()}


def choose_resource(resources: list[dict], preferred: list[str]) -> dict:
    def hay(r: dict) -> str:
        return " ".join(str(r.get(k) or "") for k in ("title", "url", "latest", "description")).lower()
    for wanted in preferred:
        w = wanted.lower()
        for r in resources:
            if w in hay(r):
                return r
    for r in resources:
        h = hay(r)
        if "place.csv" in h or ("datatourisme" in h and "place" in h and "csv" in h):
            return r
    raise RuntimeError("No DATAtourisme PLACE/region CSV resource found in official dataset metadata")


def download_resource(session: requests.Session, url: str, target: Path) -> int:
    with session.get(url, stream=True, timeout=(10, 90), allow_redirects=True) as r:
        r.raise_for_status()
        n = 0
        with target.open("wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
                    n += len(chunk)
    if n < 100:
        raise RuntimeError(f"DATAtourisme resource unexpectedly small: {n}")
    return n


def get_value(row: dict, *aliases: str) -> str:
    normalized = {norm_key(k): str(v or "") for k, v in row.items()}
    for alias in aliases:
        v = normalized.get(norm_key(alias))
        if v:
            return v
    return ""


def urls_from_row(row: dict) -> list[str]:
    preferred = []
    for aliases in (
        ("contact", "contacts", "contactpoi", "contacts_du_poi", "contactsdupoi"),
        ("website", "siteweb", "url", "siteinternet", "site_internet"),
    ):
        v = get_value(row, *aliases)
        preferred.extend(URL_RE.findall(v))
    out, seen = [], set()
    for raw in preferred:
        url = raw.rstrip(".,;:)\]")
        d = domain_of(url)
        if not d or d in PORTAL_DOMAINS or d in seen:
            continue
        seen.add(d)
        out.append(url)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--canonical-domains", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--max-candidates", type=int, default=0)
    ap.add_argument("--resource-name", default="")
    a = ap.parse_args()

    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    policy = cfg.get("policy") or {}
    max_candidates = int(a.max_candidates or policy.get("max_candidates") or 1500)
    strict_categories = [norm_key(x) for x in (cfg.get("strict_accommodation_category_keywords") or []) if x]
    excluded_categories = [norm_key(x) for x in (cfg.get("excluded_category_keywords") or []) if x]
    excluded_names = [norm_key(x) for x in (cfg.get("excluded_name_keywords") or []) if x]
    if not strict_categories:
        raise RuntimeError("DATAtourisme strict accommodation category allowlist is empty")
    preferred = list(cfg.get("preferred_resource_names") or [])
    if a.resource_name:
        preferred = [a.resource_name] + preferred

    out = Path(a.outdir)
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    canonical = read_domains(a.canonical_domains)
    session = requests.Session()
    session.headers.update({"User-Agent": "AIProd-Hospitality-DATAtourisme/1.0"})

    meta = session.get(str(cfg["dataset_api"]), timeout=(8, 30)).json()
    resources = list(meta.get("resources") or [])
    resource = choose_resource(resources, preferred)
    resource_url = str(resource.get("url") or resource.get("latest") or "")
    if not resource_url:
        rid = str(resource.get("id") or "")
        if rid:
            resource_url = f"https://www.data.gouv.fr/api/1/datasets/r/{rid}"
    if not resource_url:
        raise RuntimeError("Chosen DATAtourisme resource has no URL or id")

    raw_rows = hospitality_rows = no_site = canonical_rejected = duplicate_domain = 0
    taxonomy_rejected = taxonomy_excluded = name_excluded = 0
    rows_by_domain: dict[str, dict] = {}
    observed_types: dict[str, int] = {}

    with tempfile.TemporaryDirectory(prefix="datatourisme-") as td:
        csv_path = Path(td) / "source.csv"
        bytes_downloaded = download_resource(session, resource_url, csv_path)
        with csv_path.open("r", encoding="utf-8-sig", newline="", errors="replace") as f:
            reader = csv.DictReader(f)
            headers = list(reader.fieldnames or [])
            for row in reader:
                raw_rows += 1
                type_value = get_value(
                    row,
                    "type", "types", "categorie", "categories",
                    "categories_de_poi", "categoriesdupoi", "categorie_du_poi",
                )
                type_norm = norm_key(type_value)
                label = get_value(row, "label", "nom", "name", "titre", "nom_du_poi", "nomdupoi")
                label_norm = norm_key(label)
                if type_value:
                    for token in str(type_value).split("|")[:8]:
                        token = token.strip()
                        if token:
                            observed_types[token] = observed_types.get(token, 0) + 1
                # Precision-first quality gate. Explicit non-lodging taxonomy or
                # unmistakable campground naming wins over generic accommodation
                # tags that can coexist on multi-tagged tourism records.
                if excluded_categories and any(k in type_norm for k in excluded_categories):
                    taxonomy_excluded += 1
                    continue
                if excluded_names and any(k in label_norm for k in excluded_names):
                    name_excluded += 1
                    continue
                if not any(k in type_norm for k in strict_categories):
                    taxonomy_rejected += 1
                    continue
                hospitality_rows += 1
                urls = urls_from_row(row)
                if not urls:
                    no_site += 1
                    continue
                website = urls[0]
                domain = domain_of(website)
                if not domain:
                    no_site += 1
                    continue
                if domain in canonical:
                    canonical_rejected += 1
                    continue
                if domain in rows_by_domain:
                    duplicate_domain += 1
                    continue
                item_id = get_value(row, "id", "uri", "identifier", "uri_id_du_poi", "uriiddupoi")
                city = get_value(row, "city", "commune", "ville", "code_postal_et_commune", "codepostaletcommune")
                street = get_value(row, "street", "adresse", "address", "adresse_postale", "adressepostale")
                description = get_value(row, "description", "comment", "commentaire")
                premium = 58
                text = norm_key(" ".join((label, type_value, description)))
                if any(x in text for x in ("luxury", "luxe", "villa", "chalet", "boutique", "palace", "resort", "5etoiles", "5stars")):
                    premium = 78
                operator = 50
                if any(x in text for x in ("collection", "group", "groupe", "resorts", "hotels")):
                    operator = 66
                rows_by_domain[domain] = {
                    "source": "DATAtourisme official open data",
                    "source_family": "datatourisme_official_hospitality",
                    "source_release": str(meta.get("last_modified") or meta.get("last_update") or time.strftime("%Y-%m-%d", time.gmtime())),
                    "source_record_id": item_id,
                    "osm_type": "", "osm_id": "",
                    "country": "France", "region": "",
                    "name": label[:180], "category": type_value[:220],
                    "brand": "", "operator": "",
                    "website": website, "domain": domain,
                    "public_email": "", "email_domain": "", "email_domain_match": "", "public_phone": "",
                    "city": city[:120], "state": "", "street": street[:220],
                    "confidence": "DATATOURISME_EXPLICIT_ACCOMMODATION_TAXONOMY",
                    "operator_score": str(operator), "premium_score": str(premium),
                    "fit_tier": "A" if premium >= 75 or operator >= 65 else "B",
                    "source_url": item_id or str(cfg["dataset_api"]), "overture_id": "",
                    "notes": "Official DATAtourisme inventory; explicit commercial accommodation taxonomy plus public first-party URL.",
                    "instagram": "", "facebook": "",
                }
                if len(rows_by_domain) >= max_candidates:
                    break

    rows = list(rows_by_domain.values())
    with (out / "v6_recovery_candidates.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader(); w.writerows(rows)
    top_types = sorted(observed_types.items(), key=lambda kv: kv[1], reverse=True)[:40]
    summary = {
        "schema": "HOSPITALITY_DATATOURISME_SOURCE_V1",
        "resource_id": resource.get("id"),
        "resource_title": resource.get("title"),
        "resource_url": resource_url,
        "bytes_downloaded": bytes_downloaded,
        "raw_rows_scanned": raw_rows,
        "taxonomy_rejected": taxonomy_rejected,
        "taxonomy_excluded": taxonomy_excluded,
        "name_excluded": name_excluded,
        "hospitality_rows": hospitality_rows,
        "no_public_site": no_site,
        "canonical_known_rejected_early": canonical_rejected,
        "duplicate_domain_results": duplicate_domain,
        "canonical_unseen_candidate_domains": len(rows),
        "canonical_snapshot_domains": len(canonical),
        "headers": headers,
        "top_types": top_types,
        "elapsed_seconds": round(time.time() - t0, 2),
        "canonical_mutation": False,
    }
    (out / "datatourisme_source_summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
