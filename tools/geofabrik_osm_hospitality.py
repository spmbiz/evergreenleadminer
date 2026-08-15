#!/usr/bin/env python3
"""Extract public hospitality contacts from one official Geofabrik OSM PBF.

Uses Geofabrik's stable JSON index to resolve a small country/subregion extract.
Reads only public OSM tags. Requires a published website; explicit compatible
email -> fast lane, website-only -> first-party recovery lane. No inference.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import osmium
import requests

INDEX = "https://download.geofabrik.de/index-v1-nogeom.json"
UA = "AIProdLeadHarvester/1.0 (+public-business-research)"
KEEP_TOURISM = {"hotel", "resort", "guest_house", "apartment", "chalet", "bed_and_breakfast"}
FREE_EMAIL = {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com", "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com"}
BAD_EMAIL_DOMAINS = {"example.com", "example.org", "example.net", "booking.com", "expedia.com", "tripadvisor.com", "airbnb.com", "facebook.com", "instagram.com"}
MULTI = ("co.uk", "org.uk", "me.uk", "ltd.uk", "plc.uk", "net.uk", "com.au", "net.au", "org.au", "com.br", "com.mx", "co.nz", "net.nz", "org.nz", "co.za", "com.pt", "com.es", "com.tr", "co.jp", "com.sg", "com.hk", "com.my")
COUNTRY_DISPLAY = {"MC":"Monaco","BE":"Belgium","FR":"France","ES":"Spain","PT":"Portugal","IT":"Italy","GR":"Greece","GB":"United Kingdom","IE":"Ireland","US":"USA","CA":"Canada","MX":"Mexico","DE":"Germany","AT":"Austria","CH":"Switzerland","NL":"Netherlands","LU":"Luxembourg","HR":"Croatia","MT":"Malta","CY":"Cyprus","ME":"Montenegro","AL":"Albania","SI":"Slovenia","AU":"Australia","NZ":"New Zealand","ZA":"South Africa","MA":"Morocco","AE":"United Arab Emirates"}
FIELDS = ["source","source_family","source_release","source_record_id","osm_type","osm_id","country","region","name","category","brand","operator","website","domain","public_email","email_domain","email_domain_match","public_phone","city","state","street","confidence","operator_score","premium_score","fit_tier","source_url","overture_id","notes","instagram","facebook"]
STRIP_CHARS = "<>[](){}.,;:\"'"


def norm(v):
    return re.sub(r"\s+", " ", str(v or "")).strip()


def root_host(h):
    h = (h or "").lower().strip(".")
    if h.startswith("www."):
        h = h[4:]
    if not h:
        return ""
    for suffix in MULTI:
        if h == suffix:
            return h
        if h.endswith("." + suffix):
            return ".".join(h.split(".")[-3:])
    parts = h.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else h


def host(url):
    try:
        return root_host((urlparse(url).hostname or "").lower())
    except Exception:
        return ""


def normalize_url(v):
    u = norm(v)
    if not u:
        return ""
    if not re.match(r"^https?://", u, re.I):
        if re.match(r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}(/|$)", u):
            u = "https://" + u
        else:
            return ""
    try:
        p = urlparse(u)
        return u if p.scheme in ("http", "https") and p.hostname else ""
    except Exception:
        return ""


def email_domain(email):
    e = norm(email).lower().strip(STRIP_CHARS)
    return e.rsplit("@", 1)[1] if "@" in e else ""


def valid_email(email):
    e = norm(email).lower().strip(STRIP_CHARS)
    if not re.fullmatch(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", e):
        return False
    d = email_domain(e)
    if d in BAD_EMAIL_DOMAINS or any(d.endswith("." + x) for x in BAD_EMAIL_DOMAINS):
        return False
    return not any(x in e for x in ("example@", "test@", "noreply@", "no-reply@"))


def resolve_extract(extract_id, iso2):
    r = requests.get(INDEX, timeout=30, headers={"User-Agent": UA})
    r.raise_for_status()
    candidates = []
    for feature in r.json().get("features") or []:
        props = feature.get("properties") or {}
        url = (props.get("urls") or {}).get("pbf")
        if not url:
            continue
        if extract_id and props.get("id") == extract_id:
            return props, url
        codes = props.get("iso3166-1:alpha2") or []
        if iso2 and iso2.upper() in codes:
            candidates.append((props, url))
    if not candidates:
        raise RuntimeError(f"Geofabrik extract not found id={extract_id} iso2={iso2}")
    candidates.sort(key=lambda x: (-str(x[0].get("id") or "").count("/"), len(str(x[0].get("id") or ""))))
    return candidates[0]


def download(url, path, max_bytes):
    total = 0
    with requests.get(url, stream=True, timeout=90, headers={"User-Agent": UA}) as r:
        r.raise_for_status()
        content_length = int(r.headers.get("Content-Length") or 0)
        if content_length and content_length > max_bytes:
            raise RuntimeError(f"extract too large: {content_length} > {max_bytes}")
        with path.open("wb") as f:
            for chunk in r.iter_content(1024 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise RuntimeError(f"extract exceeded cap: {total} > {max_bytes}")
                f.write(chunk)
    return total


class Handler(osmium.SimpleHandler):
    def __init__(self, country, region):
        super().__init__()
        self.country = country
        self.region = region
        self.rows = []
        self.seen = set()

    def node(self, obj):
        self._obj(obj, "node")

    def way(self, obj):
        self._obj(obj, "way")

    def relation(self, obj):
        self._obj(obj, "relation")

    def _obj(self, obj, kind):
        tags = {x.k: x.v for x in obj.tags}
        tourism = norm(tags.get("tourism")).lower()
        if tourism not in KEEP_TOURISM:
            return
        name = norm(tags.get("name") or tags.get("brand") or tags.get("operator"))
        website = normalize_url(tags.get("contact:website") or tags.get("website") or tags.get("url"))
        if not name or not website:
            return
        domain = host(website)
        if not domain or domain in self.seen:
            return
        email = norm(tags.get("contact:email") or tags.get("email")).lower().strip(STRIP_CHARS)
        if email and not valid_email(email):
            email = ""
        email_root = root_host(email_domain(email)) if email else ""
        if email and email_root != domain and email_root not in FREE_EMAIL:
            email = ""
            email_root = ""
        phone = norm(tags.get("contact:phone") or tags.get("phone"))
        city = norm(tags.get("addr:city"))
        state = norm(tags.get("addr:state"))
        street = " ".join(x for x in (norm(tags.get("addr:housenumber")), norm(tags.get("addr:street"))) if x)
        brand = norm(tags.get("brand"))
        operator = norm(tags.get("operator"))
        premium = 80 if tourism == "resort" else 70 if tourism in ("hotel", "chalet") else 58
        operator_score = 65 if operator else 45
        oid = str(obj.id)
        source_url = f"https://www.openstreetmap.org/{kind}/{oid}"
        row = {
            "source":"OpenStreetMap via Geofabrik",
            "source_family":"openstreetmap_geofabrik",
            "source_release":self.region,
            "source_record_id":f"osm:{kind}:{oid}",
            "osm_type":kind,
            "osm_id":oid,
            "country":self.country,
            "region":self.region,
            "name":name,
            "category":tourism,
            "brand":brand,
            "operator":operator,
            "website":website,
            "domain":domain,
            "public_email":email,
            "email_domain":email_domain(email),
            "email_domain_match":"YES" if email and email_root == domain else ("FREE_WEBMAIL" if email else ""),
            "public_phone":phone,
            "city":city,
            "state":state,
            "street":street,
            "confidence":"OSM_PUBLIC_TAGS",
            "operator_score":str(operator_score),
            "premium_score":str(premium),
            "fit_tier":"A" if premium >= 70 or operator_score >= 65 else "B",
            "source_url":source_url,
            "overture_id":"",
            "notes":"Public OSM tourism/contact tags via official Geofabrik extract; no inference.",
            "instagram":normalize_url(tags.get("contact:instagram")),
            "facebook":normalize_url(tags.get("contact:facebook"))
        }
        self.seen.add(domain)
        self.rows.append(row)


def write_csv(path, rows):
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--extract-id", default="")
    ap.add_argument("--iso2", default="")
    ap.add_argument("--country", default="")
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--max-download-mb", type=int, default=800)
    args = ap.parse_args()
    t0 = time.time()
    props, url = resolve_extract(args.extract_id, args.iso2)
    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    pbf = out / "source.osm.pbf"
    size = download(url, pbf, args.max_download_mb * 1024 * 1024)
    codes = props.get("iso3166-1:alpha2") or []
    iso2 = (args.iso2 or (codes[0] if codes else "")).upper()
    country = args.country or COUNTRY_DISPLAY.get(iso2, iso2)
    region = str(props.get("id") or args.extract_id or iso2)
    handler = Handler(country, region)
    handler.apply_file(str(pbf), locations=False)
    pbf.unlink(missing_ok=True)
    fast = [r for r in handler.rows if r["public_email"]]
    recovery = [r for r in handler.rows if not r["public_email"]]
    write_csv(out / "v6_fast_ready.csv", fast)
    write_csv(out / "v6_recovery_candidates.csv", recovery)
    summary = {"extract_id":region,"iso2":iso2,"country":country,"pbf_url":url,"download_bytes":size,"hospitality_domains":len(handler.rows),"fast_ready":len(fast),"recovery_candidates":len(recovery),"elapsed_seconds":round(time.time()-t0, 2)}
    (out / "osm_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
