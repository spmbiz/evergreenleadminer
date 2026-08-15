#!/usr/bin/env python3
"""Normalize one official AllThePlaces spider into hospitality discovery lanes.

AllThePlaces offers a documented per-spider endpoint:
  /runs/latest/output/{spider}.geojson
which redirects to that spider's latest successful output. We persist the actual
redirected run URL because it may be older than the global ATP run.

Provenance modes:
- first_party: published website is first-party; explicit email is optional.
- trusted_directory_contact: directory listing publishes a member email. The
  directory URL remains provenance only. A non-free email domain becomes a
  candidate official domain and MUST pass downstream live verification.
- roster_only: premium/property evidence only; never canonical-ready directly.

No email patterns are inferred. No authentication, forms or anti-bot bypass.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import ijson
import requests

API_ROOT = "https://data.alltheplaces.xyz"
CONFIG = Path(__file__).resolve().parents[1] / "config/atp_hospitality_spiders.json"
UA = "AIProdLeadHarvester/1.0 (+public-business-research)"
MULTI_SUFFIXES = (
    "co.uk","org.uk","me.uk","ltd.uk","plc.uk","net.uk","com.au","net.au","org.au",
    "com.br","com.mx","co.nz","net.nz","org.nz","co.za","com.pt","com.es","com.tr",
    "co.jp","com.sg","com.hk","com.my"
)
FREE_EMAIL = {
    "gmail.com","googlemail.com","outlook.com","hotmail.com","live.com","yahoo.com","icloud.com",
    "me.com","aol.com","proton.me","protonmail.com"
}
BAD_EMAIL_DOMAINS = {
    "example.com","example.org","example.net","sentry.io","cloudflare.com","wixpress.com",
    "squarespace.com","wordpress.com","mailchimp.com","hubspot.com","booking.com","expedia.com",
    "tripadvisor.com","airbnb.com","facebook.com","instagram.com"
}
FIELDS = [
    "source","source_family","source_release","source_record_id","atp_id","atp_spider","atp_mode",
    "country","region","name","category","brand","operator","website","domain","directory_url",
    "public_email","email_domain","email_domain_match","public_phone","city","state","street",
    "confidence","operator_score","premium_score","fit_tier","source_url","overture_id","notes",
    "instagram","facebook"
]
COUNTRY_DISPLAY = {
    "US":"USA","CA":"Canada","MX":"Mexico","GB":"United Kingdom","IE":"Ireland","FR":"France","ES":"Spain",
    "PT":"Portugal","IT":"Italy","GR":"Greece","DE":"Germany","AT":"Austria","CH":"Switzerland",
    "NL":"Netherlands","BE":"Belgium","LU":"Luxembourg","DK":"Denmark","NO":"Norway","SE":"Sweden",
    "FI":"Finland","IS":"Iceland","PL":"Poland","CZ":"Czechia","SK":"Slovakia","HU":"Hungary",
    "SI":"Slovenia","HR":"Croatia","ME":"Montenegro","AL":"Albania","MT":"Malta","CY":"Cyprus",
    "RO":"Romania","BG":"Bulgaria","RS":"Serbia","BA":"Bosnia and Herzegovina","MK":"North Macedonia",
    "EE":"Estonia","LV":"Latvia","LT":"Lithuania","TR":"Turkey","GE":"Georgia","AE":"United Arab Emirates",
    "SA":"Saudi Arabia","QA":"Qatar","BH":"Bahrain","OM":"Oman","JO":"Jordan","MA":"Morocco","TN":"Tunisia",
    "EG":"Egypt","AU":"Australia","NZ":"New Zealand","JP":"Japan","KR":"South Korea","TH":"Thailand",
    "VN":"Vietnam","MY":"Malaysia","SG":"Singapore","ID":"Indonesia","PH":"Philippines","LK":"Sri Lanka",
    "MV":"Maldives","IN":"India","CN":"China","HK":"Hong Kong","TW":"Taiwan","ZA":"South Africa",
    "MU":"Mauritius","SC":"Seychelles","KE":"Kenya","TZ":"Tanzania","NA":"Namibia","MZ":"Mozambique",
    "MG":"Madagascar","BR":"Brazil","AR":"Argentina","CL":"Chile","UY":"Uruguay","CO":"Colombia",
    "PE":"Peru","EC":"Ecuador","BS":"Bahamas","JM":"Jamaica","DO":"Dominican Republic","KY":"Cayman Islands",
    "TC":"Turks and Caicos Islands","BB":"Barbados","AW":"Aruba","CW":"Curaçao","LC":"Saint Lucia",
    "AG":"Antigua and Barbuda","GD":"Grenada","BZ":"Belize","CR":"Costa Rica","PA":"Panama","GT":"Guatemala",
    "HN":"Honduras","NI":"Nicaragua","SV":"El Salvador","PR":"Puerto Rico","VI":"US Virgin Islands","AD":"Andorra"
}


def norm(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return norm(v[0]) if v else ""
    if isinstance(v, dict):
        return ""
    return re.sub(r"\s+", " ", str(v)).strip()


def root_host(h: str) -> str:
    h = (h or "").lower().strip(".")
    if h.startswith("www."):
        h = h[4:]
    if not h:
        return ""
    for suffix in MULTI_SUFFIXES:
        if h == suffix:
            return h
        if h.endswith("." + suffix):
            p = h.split(".")
            return ".".join(p[-3:])
    p = h.split(".")
    return ".".join(p[-2:]) if len(p) >= 2 else h


def url_domain(u: str) -> str:
    try:
        return root_host((urlparse(u).hostname or "").lower())
    except Exception:
        return ""


def valid_url(u: str) -> str:
    u = norm(u)
    if not u:
        return ""
    if not re.match(r"^https?://", u, re.I):
        return ""
    try:
        p = urlparse(u)
        return u if p.scheme in ("http", "https") and p.hostname else ""
    except Exception:
        return ""


def email_domain(e: str) -> str:
    e = norm(e).lower().strip("<>[](){}.,;:\"'")
    return e.rsplit("@", 1)[1] if "@" in e else ""


def valid_email(e: str) -> bool:
    e = norm(e).lower().strip("<>[](){}.,;:\"'")
    if not re.fullmatch(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", e):
        return False
    d = email_domain(e)
    if d in BAD_EMAIL_DOMAINS or any(d.endswith("." + x) for x in BAD_EMAIL_DOMAINS):
        return False
    return not any(x in e for x in ("example@","test@","noreply@","no-reply@","donotreply@"))


def pick_email(props: dict) -> str:
    for key in ("email", "contact:email"):
        v = props.get(key)
        vals = v if isinstance(v, (list, tuple)) else re.split(r"[,;\s]+", norm(v))
        for x in vals:
            e = norm(x).lower().strip("<>[](){}.,;:\"'")
            if valid_email(e):
                return e
    return ""


def prop(props: dict, *keys):
    for k in keys:
        if k in props and props[k] not in (None, "", [], {}):
            return props[k]
    return ""


def display_country(v) -> str:
    c = norm(v).upper()
    return COUNTRY_DISPLAY.get(c, c)


def spider_policy(spider: str) -> dict:
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    for s in cfg.get("spiders") or []:
        if s.get("spider") == spider:
            return s
    raise RuntimeError(f"spider not present in source policy: {spider}")


def parse_actual_run(url: str) -> str:
    m = re.search(r"/runs/([^/]+)/output/", url)
    return m.group(1) if m else ""


def stable_id(spider: str, actual_run: str, ref: str, source_url: str, name: str) -> str:
    material = "|".join((spider, actual_run, ref, source_url, name))
    return "atp:" + hashlib.sha1(material.encode("utf-8")).hexdigest()[:24]


def normalize_feature(feature: dict, spider: str, mode: str, actual_run: str):
    props = feature.get("properties") if isinstance(feature, dict) else None
    if not isinstance(props, dict):
        return None, "missing_properties"
    name = norm(prop(props, "name", "branch"))
    brand = norm(prop(props, "brand"))
    operator = norm(prop(props, "operator"))
    if not name:
        # A brand is not a safe substitute for a missing property identity in a
        # multi-property spider; preserve as unresolved evidence instead.
        return None, "missing_name"
    tourism = norm(prop(props, "tourism")).lower().replace("_", " ")
    category = tourism or norm(prop(props, "category", "amenity"))
    if tourism and tourism not in ("hotel","resort","guest house","apartment","chalet","bed and breakfast","inn","holiday apartment"):
        return None, "non_hospitality"

    published_site = valid_url(prop(props, "website", "contact:website", "url"))
    source_url = valid_url(prop(props, "@source_uri", "source_uri")) or published_site
    ref = norm(prop(props, "ref", "id", "@id")) or norm(feature.get("id"))
    email = pick_email(props)
    email_root = root_host(email_domain(email)) if email else ""
    listing_root = url_domain(published_site)

    website = ""
    domain = ""
    confidence = ""
    note = ""
    ready_class = "roster"

    if mode == "first_party":
        website = published_site
        domain = listing_root
        if not website or not domain:
            return None, "missing_first_party_site"
        if email:
            eroot = root_host(email_domain(email))
            if eroot != domain and eroot not in FREE_EMAIL:
                # Keep the property as site-recovery rather than attaching an
                # unrelated or parent-domain address to a property silently.
                email = ""
                email_root = ""
        ready_class = "fast" if email else "recovery"
        confidence = "ATP_FIRST_PARTY"
        note = "ATP first-party property/operator page; explicit public fields only."

    elif mode == "trusted_directory_contact":
        if not email or not email_root or email_root in FREE_EMAIL:
            return None, "directory_contact_without_resolvable_business_domain"
        # This is deliberately only a candidate URL. It is canonical-ready only
        # after the downstream live verifier proves current hospitality identity.
        website = "https://" + email_root
        domain = email_root
        ready_class = "fast"
        confidence = "ATP_DIRECTORY_EMAIL_DOMAIN_CANDIDATE"
        note = "Trusted public directory publishes member email. Candidate official website derived from published business email domain; MUST pass live first-party identity verification before canonicalization."

    elif mode == "roster_only":
        website = published_site
        domain = ""
        email = ""
        email_root = ""
        ready_class = "roster"
        confidence = "ATP_ROSTER_ONLY"
        note = "Premium public roster evidence only. Directory website is provenance, never canonical member domain."
    else:
        return None, "unknown_mode"

    city = norm(prop(props, "addr:city", "city", "locality"))
    state = norm(prop(props, "addr:state", "state", "region"))
    country = display_country(prop(props, "addr:country", "country"))
    street = norm(prop(props, "addr:full", "street_address", "address"))
    if not street:
        street = " ".join(x for x in (norm(prop(props, "addr:housenumber")), norm(prop(props, "addr:street"))) if x)
    phone = norm(prop(props, "phone", "contact:phone"))
    premium = 70 if brand or tourism in ("hotel", "resort") else 55
    operator_score = 65 if operator else 45
    if mode == "trusted_directory_contact":
        premium = max(premium, 75)
    fit_tier = "A" if premium >= 70 or operator_score >= 65 else "B"
    row = {
        "source":"AllThePlaces per-spider GeoJSON",
        "source_family":"alltheplaces",
        "source_release":actual_run,
        "source_record_id":stable_id(spider, actual_run, ref, source_url, name),
        "atp_id":ref,
        "atp_spider":spider,
        "atp_mode":mode,
        "country":country,
        "region":f"ATP::{spider}",
        "name":name,
        "category":category,
        "brand":brand,
        "operator":operator,
        "website":website,
        "domain":domain,
        "directory_url":published_site if mode != "first_party" else "",
        "public_email":email,
        "email_domain":email_domain(email),
        "email_domain_match":"YES" if email and domain and root_host(email_domain(email)) == domain else "",
        "public_phone":phone,
        "city":city,
        "state":state,
        "street":street,
        "confidence":confidence,
        "operator_score":str(operator_score),
        "premium_score":str(premium),
        "fit_tier":fit_tier,
        "source_url":source_url,
        "overture_id":"",
        "notes":note,
        "instagram":"",
        "facebook":""
    }
    return row, ready_class


def write_csv(path: Path, rows: list[dict]):
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spider", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--timeout", type=float, default=45.0)
    ap.add_argument("--max-features", type=int, default=0, help="canary cap; 0 = complete spider")
    a = ap.parse_args()

    t0 = time.time()
    policy = spider_policy(a.spider)
    mode = str(policy.get("mode") or "")
    url = f"{API_ROOT}/runs/latest/output/{a.spider}.geojson"
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept":"application/geo+json,application/json;q=0.9,*/*;q=0.1"})
    outdir = Path(a.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    fast, recovery, roster = [], [], []
    seen = {"fast":set(), "recovery":set(), "roster":set()}
    rejects = {}
    features = 0
    with s.get(url, stream=True, timeout=a.timeout, allow_redirects=True) as r:
        r.raise_for_status()
        actual_url = r.url
        actual_run = parse_actual_run(actual_url)
        r.raw.decode_content = True
        for feature in ijson.items(r.raw, "features.item"):
            features += 1
            row, bucket = normalize_feature(feature, a.spider, mode, actual_run)
            if not row:
                rejects[bucket] = rejects.get(bucket, 0) + 1
            else:
                dedupe = row.get("domain") or row.get("source_record_id")
                if dedupe in seen[bucket]:
                    rejects["duplicate_within_spider"] = rejects.get("duplicate_within_spider", 0) + 1
                else:
                    seen[bucket].add(dedupe)
                    {"fast":fast,"recovery":recovery,"roster":roster}[bucket].append(row)
            if a.max_features and features >= a.max_features:
                break

    fast.sort(key=lambda x:(x["fit_tier"]!="A", x["name"].lower()))
    recovery.sort(key=lambda x:(x["fit_tier"]!="A", x["name"].lower()))
    roster.sort(key=lambda x:(x["fit_tier"]!="A", x["name"].lower()))
    write_csv(outdir / "atp_fast_ready.csv", fast)
    write_csv(outdir / "atp_recovery_candidates.csv", recovery)
    write_csv(outdir / "atp_roster_only.csv", roster)
    # Compatibility aliases for existing gates.
    write_csv(outdir / "v6_fast_ready.csv", fast)
    write_csv(outdir / "v6_recovery_candidates.csv", recovery)

    summary = {
        "spider":a.spider,
        "mode":mode,
        "request_url":url,
        "actual_output_url":actual_url,
        "actual_run":actual_run,
        "features_seen":features,
        "fast_ready":len(fast),
        "recovery_candidates":len(recovery),
        "roster_only":len(roster),
        "rejects":rejects,
        "partial_canary":bool(a.max_features),
        "elapsed_seconds":round(time.time()-t0, 2)
    }
    (outdir / "atp_spider_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
