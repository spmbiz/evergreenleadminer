#!/usr/bin/env python3
"""Bulk hospitality lead discovery from Geofabrik/OpenStreetMap PBF extracts.

Pipeline:
  PBF -> osmium tag filter -> GeoJSON sequence -> normalize/dedupe ->
  lightweight site enrichment -> CSV/JSON artifacts.

This script only extracts public business information. It never guesses email
addresses. Website enrichment reads the homepage plus a small number of public
same-domain contact/about/reservations pages and stores only emails visibly
published in HTML/mailto links.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

USER_AGENT = "EvergreenLeadMiner-Bulk/2.0 (+public-business-research)"
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.8",
}

EMAIL_RE = re.compile(
    r"(?<![A-Z0-9._%+-])([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})(?![A-Z0-9._%+-])",
    re.I,
)

BAD_EMAIL_PARTS = {
    "example.com",
    "example.org",
    "sentry.io",
    "wixpress.com",
    "cloudflare.com",
    "wordpress.org",
    "schema.org",
}

CONTACT_HINTS = (
    "contact",
    "about",
    "reservation",
    "reservations",
    "booking",
    "book",
    "stay",
    "reach-us",
    "get-in-touch",
)

LUXURY_HINTS = (
    "luxury",
    "boutique",
    "villa",
    "villas",
    "resort",
    "retreat",
    "lodge",
    "lodges",
    "suites",
    "residence",
    "residences",
    "chalet",
    "chalets",
    "vacation",
    "holiday",
    "beachfront",
    "oceanfront",
    "spa",
    "collection",
)

TAG_EXPRESSIONS = [
    "nwr/tourism=hotel,guest_house,apartment,chalet,hostel,motel",
    "nwr/leisure=resort",
]

CSV_FIELDS = [
    "source",
    "osm_id",
    "country",
    "region",
    "name",
    "operator",
    "brand",
    "hospitality_type",
    "city",
    "state",
    "postcode",
    "street",
    "housenumber",
    "website",
    "domain",
    "public_email_osm",
    "public_email_web",
    "email_source_url",
    "public_phone",
    "rooms",
    "beds",
    "stars",
    "score",
    "priority",
    "source_url",
    "notes",
]


def run(cmd: Sequence[str]) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(list(cmd), check=True)


def norm_text(value: object) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def norm_website(value: object) -> str:
    v = norm_text(value)
    if not v:
        return ""
    # OSM occasionally stores several URLs separated by ; . Keep first public URL.
    v = v.split(";")[0].strip()
    if not re.match(r"^https?://", v, re.I):
        v = "https://" + v.lstrip("/")
    try:
        p = urlparse(v)
        if not p.netloc or "." not in p.netloc:
            return ""
        return p._replace(fragment="").geturl()
    except Exception:
        return ""


def root_domain(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower().strip(".")
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return ""


def first_tag(tags: Dict[str, object], keys: Sequence[str]) -> str:
    for key in keys:
        val = norm_text(tags.get(key))
        if val:
            return val
    return ""


def parse_number(value: str) -> Optional[float]:
    if not value:
        return None
    m = re.search(r"\d+(?:\.\d+)?", value.replace(",", "."))
    if not m:
        return None
    try:
        return float(m.group(0))
    except Exception:
        return None


def public_osm_email(tags: Dict[str, object]) -> str:
    raw = first_tag(tags, ["contact:email", "email"])
    if not raw:
        return ""
    for piece in re.split(r"[;,\s]+", raw):
        piece = piece.strip().strip("<>[](){}.,;:")
        if EMAIL_RE.fullmatch(piece):
            return piece
    return ""


def hospitality_type(tags: Dict[str, object]) -> str:
    if norm_text(tags.get("leisure")) == "resort":
        return "resort"
    return norm_text(tags.get("tourism")) or "hospitality"


def score_row(row: Dict[str, str]) -> int:
    typ = row["hospitality_type"].lower()
    score = {
        "resort": 35,
        "hotel": 28,
        "apartment": 22,
        "chalet": 24,
        "guest_house": 14,
        "motel": 10,
        "hostel": 6,
    }.get(typ, 10)

    if row["website"]:
        score += 24
    if row["public_email_osm"]:
        score += 28
    if row["public_phone"]:
        score += 9
    if row["operator"] or row["brand"]:
        score += 10

    rooms = parse_number(row["rooms"])
    if rooms is not None:
        if rooms >= 50:
            score += 14
        elif rooms >= 15:
            score += 9
        elif rooms >= 5:
            score += 4

    stars = parse_number(row["stars"])
    if stars is not None:
        if stars >= 5:
            score += 12
        elif stars >= 4:
            score += 8

    hay = " ".join([row["name"], row["operator"], row["brand"], row["website"]]).lower()
    keyword_hits = sum(1 for k in LUXURY_HINTS if k in hay)
    score += min(keyword_hits * 4, 16)

    return min(score, 100)


def priority(score: int) -> str:
    if score >= 70:
        return "A"
    if score >= 48:
        return "B"
    return "C"


def make_key(row: Dict[str, str]) -> str:
    if row["domain"]:
        return "domain:" + row["domain"]
    bits = [row["name"].lower(), row["city"].lower(), row["state"].lower()]
    return "namegeo:" + "|".join(re.sub(r"[^a-z0-9]+", "", b) for b in bits)


def extract_candidates(pbf: Path, outdir: Path, country: str, region: str, max_candidates: int) -> List[Dict[str, str]]:
    filtered = outdir / "hospitality.filtered.osm.pbf"
    seq = outdir / "hospitality.geojsonseq"

    run(["osmium", "tags-filter", "-t", "-O", "-o", str(filtered), str(pbf), *TAG_EXPRESSIONS])
    run([
        "osmium",
        "export",
        "-O",
        "-f",
        "geojsonseq",
        "-x",
        "print_record_separator=false",
        "-u",
        "type_id",
        "-o",
        str(seq),
        str(filtered),
    ])

    seen = set()
    rows: List[Dict[str, str]] = []
    with seq.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip().lstrip("\x1e")
            if not line:
                continue
            try:
                feat = json.loads(line)
            except Exception:
                continue
            props = feat.get("properties") or {}
            if not isinstance(props, dict):
                continue
            tags = props
            name = norm_text(tags.get("name"))
            if not name:
                continue

            website = first_tag(tags, ["website", "contact:website", "url", "contact:url"])
            website = norm_website(website)
            domain = root_domain(website) if website else ""
            osm_uid = norm_text(tags.get("@id")) or norm_text(feat.get("id"))

            row: Dict[str, str] = {
                "source": "openstreetmap/geofabrik",
                "osm_id": osm_uid,
                "country": country,
                "region": region,
                "name": name,
                "operator": first_tag(tags, ["operator"]),
                "brand": first_tag(tags, ["brand"]),
                "hospitality_type": hospitality_type(tags),
                "city": first_tag(tags, ["addr:city", "addr:place", "is_in:city"]),
                "state": first_tag(tags, ["addr:state", "is_in:state"]),
                "postcode": first_tag(tags, ["addr:postcode"]),
                "street": first_tag(tags, ["addr:street"]),
                "housenumber": first_tag(tags, ["addr:housenumber"]),
                "website": website,
                "domain": domain,
                "public_email_osm": public_osm_email(tags),
                "public_email_web": "",
                "email_source_url": "",
                "public_phone": first_tag(tags, ["contact:phone", "phone", "contact:mobile", "mobile"]),
                "rooms": first_tag(tags, ["rooms"]),
                "beds": first_tag(tags, ["beds"]),
                "stars": first_tag(tags, ["stars"]),
                "score": "0",
                "priority": "C",
                "source_url": f"https://www.openstreetmap.org/{osm_uid}" if osm_uid else "",
                "notes": "",
            }
            s = score_row(row)
            row["score"] = str(s)
            row["priority"] = priority(s)
            key = make_key(row)
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
            if max_candidates and len(rows) >= max_candidates:
                break

    rows.sort(key=lambda r: int(r["score"]), reverse=True)
    return rows


def clean_email(value: str) -> str:
    value = html.unescape(value or "")
    value = value.replace("mailto:", "").split("?")[0].strip()
    value = value.strip("<>[](){}.,;:'\"")
    if not EMAIL_RE.fullmatch(value):
        return ""
    low = value.lower()
    if any(bad in low for bad in BAD_EMAIL_PARTS):
        return ""
    if low.endswith((".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif")):
        return ""
    return value


def extract_emails(text: str, soup: BeautifulSoup) -> List[str]:
    found = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = str(a.get("href") or "")
        if href.lower().startswith("mailto:"):
            e = clean_email(href)
            if e and e.lower() not in seen:
                seen.add(e.lower())
                found.append(e)
    for match in EMAIL_RE.findall(text or ""):
        e = clean_email(match)
        if e and e.lower() not in seen:
            seen.add(e.lower())
            found.append(e)
    return found


def same_domain_links(base_url: str, soup: BeautifulSoup, max_links: int = 3) -> List[str]:
    base_host = root_domain(base_url)
    scored: List[Tuple[int, str]] = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = str(a.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = urljoin(base_url, href)
        if root_domain(url) != base_host:
            continue
        p = urlparse(url)
        clean = p._replace(fragment="").geturl()
        if clean in seen:
            continue
        seen.add(clean)
        hay = (clean + " " + norm_text(a.get_text(" ", strip=True))).lower()
        rank = 0
        for i, hint in enumerate(CONTACT_HINTS):
            if hint in hay:
                rank += 20 - min(i, 10)
        if rank:
            scored.append((rank, clean))
    scored.sort(reverse=True)
    return [u for _, u in scored[:max_links]]


def fetch_html(url: str, timeout: int = 12) -> Tuple[str, str]:
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        if r.status_code >= 400:
            return "", ""
        ctype = (r.headers.get("content-type") or "").lower()
        if "html" not in ctype and "xhtml" not in ctype and ctype:
            return "", ""
        return r.url, r.text[:2_500_000]
    except Exception:
        return "", ""


def enrich_one(row: Dict[str, str]) -> Tuple[str, str, str]:
    website = row.get("website") or ""
    if not website:
        return "", "", "no_website"
    if row.get("public_email_osm"):
        return "", "", "email_already_in_osm"

    visited = []
    final_url, body = fetch_html(website)
    if not body and website.startswith("https://"):
        final_url, body = fetch_html("http://" + website[len("https://") :])
    if not body:
        return "", "", "homepage_fetch_failed"

    visited.append(final_url or website)
    soup = BeautifulSoup(body, "html.parser")
    emails = extract_emails(body, soup)
    if emails:
        return emails[0], visited[-1], "homepage"

    for link in same_domain_links(final_url or website, soup, max_links=3):
        u, txt = fetch_html(link)
        if not txt:
            continue
        visited.append(u or link)
        sp = BeautifulSoup(txt, "html.parser")
        emails = extract_emails(txt, sp)
        if emails:
            return emails[0], visited[-1], "contact_page"

    return "", "", "no_public_email_found"


def enrich_sites(rows: List[Dict[str, str]], max_enrich: int, workers: int) -> Dict[str, int]:
    candidates = [r for r in rows if r["website"] and not r["public_email_osm"]]
    candidates.sort(key=lambda r: (r["priority"] != "A", -int(r["score"])))
    if max_enrich > 0:
        candidates = candidates[:max_enrich]

    counts = {
        "attempted": len(candidates),
        "public_email_found_web": 0,
        "already_had_osm_email": sum(1 for r in rows if r["public_email_osm"]),
    }
    if not candidates:
        return counts

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futures = {ex.submit(enrich_one, row): row for row in candidates}
        done = 0
        for fut in as_completed(futures):
            row = futures[fut]
            done += 1
            try:
                email, source_url, note = fut.result()
            except Exception as e:
                email, source_url, note = "", "", f"error:{type(e).__name__}"
            if email:
                row["public_email_web"] = email
                row["email_source_url"] = source_url
                counts["public_email_found_web"] += 1
                # Verified public email materially improves contactability score.
                new_score = min(100, int(row["score"]) + 24)
                row["score"] = str(new_score)
                row["priority"] = priority(new_score)
            row["notes"] = (row["notes"] + "; " + note).strip("; ")
            if done % 100 == 0:
                print(f"Enriched {done}/{len(candidates)} sites; web emails={counts['public_email_found_web']}", flush=True)

    rows.sort(key=lambda r: int(r["score"]), reverse=True)
    return counts


def write_csv(path: Path, rows: Iterable[Dict[str, str]]) -> int:
    n = 0
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow(row)
            n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pbf", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--country", default="")
    ap.add_argument("--region", default="")
    ap.add_argument("--max-candidates", type=int, default=25000)
    ap.add_argument("--max-enrich", type=int, default=2500)
    ap.add_argument("--workers", type=int, default=32)
    args = ap.parse_args()

    if not shutil_which("osmium"):
        print("ERROR: osmium CLI is required", file=sys.stderr)
        return 2

    pbf = Path(args.pbf)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    started = time.time()
    rows = extract_candidates(pbf, outdir, args.country, args.region, args.max_candidates)
    print(f"Unique hospitality candidates: {len(rows)}", flush=True)

    enrichment = enrich_sites(rows, args.max_enrich, args.workers)

    raw_count = write_csv(outdir / "candidates_all.csv", rows)
    website_rows = [r for r in rows if r["website"]]
    email_rows = [r for r in rows if r["public_email_osm"] or r["public_email_web"]]
    ab_rows = [r for r in rows if r["priority"] in {"A", "B"}]
    a_rows = [r for r in rows if r["priority"] == "A"]

    write_csv(outdir / "candidates_with_website.csv", website_rows)
    write_csv(outdir / "candidates_with_public_email.csv", email_rows)
    write_csv(outdir / "priority_ab.csv", ab_rows)
    write_csv(outdir / "priority_a.csv", a_rows)

    summary = {
        "country": args.country,
        "region": args.region,
        "raw_unique_candidates": raw_count,
        "with_website": len(website_rows),
        "with_public_email": len(email_rows),
        "priority_a": len(a_rows),
        "priority_ab": len(ab_rows),
        "osm_public_email": enrichment["already_had_osm_email"],
        "web_enrichment_attempted": enrichment["attempted"],
        "web_public_email_found": enrichment["public_email_found_web"],
        "elapsed_seconds": round(time.time() - started, 2),
        "rules": {
            "emails": "published only; never inferred",
            "dedupe": "domain first, otherwise normalized name+city+state within shard",
            "source": "OpenStreetMap data via Geofabrik + public first-party websites",
        },
    }
    (outdir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)
    return 0


def shutil_which(name: str) -> Optional[str]:
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = Path(d) / name
        if p.exists() and os.access(p, os.X_OK):
            return str(p)
    return None


if __name__ == "__main__":
    raise SystemExit(main())
