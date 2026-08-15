#!/usr/bin/env python3
"""Overture V6 website-first recovery discovery.

Find commercially relevant hospitality/operator places that have a public website
but do not already expose a usable email in Overture. This stage performs no
per-lead HTTP. It emits one candidate per registrable website domain for the
bounded first-party contact crawler.

Nothing is inferred. World-atlas cells may pass --country AUTO; country is then
read from the Overture address record.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import duckdb

FIELDS = [
    "source","overture_id","country","region","name","category","brand",
    "website","domain","public_email","email_domain","email_domain_match",
    "public_phone","city","state","street","confidence","operator_score",
    "premium_score","fit_tier","source_url","notes"
]

OPERATOR = (
    "vacation rental","vacation rentals","vacation home","vacation homes",
    "holiday rental","holiday rentals","holiday home","holiday homes","holiday let","holiday lets",
    "villa rental","villa rentals","villa management","property management","rental management",
    "short term rental","short-term rental","short stay","short-stay","serviced apartment",
    "serviced apartments","serviced accommodation","aparthotel","apartment hotel","luxury rentals",
    "managed homes","managed properties","condo rentals","cabin rentals","chalet rentals",
    "beach rentals","vacation property","self catering","self-catering","rental agency",
    "holiday cottages","property manager","vacation management"
)
PREMIUM = (
    "luxury","boutique","villa","villas","resort","retreat","estate","residence","residences",
    "beachfront","oceanfront","waterfront","ski","chalet","penthouse","private island","collection",
    "lodge","spa hotel","country house","country resort","eco resort","glamping"
)
HARD_REJECT = (
    "hostel","backpacker","motel 6","super 8","econo lodge","econolodge","rodeway inn",
    "quality inn","comfort inn","days inn","red roof","budget inn","student housing","senior living",
    "assisted living","campground","rv park","rv resort","timeshare sales","wedding planner"
)
BAD_EMAIL_DOMAINS = {
    "example.com","example.org","example.net","sentry.io","cloudflare.com","wixpress.com",
    "squarespace.com","wordpress.com","mailchimp.com","hubspot.com","booking.com","expedia.com",
    "tripadvisor.com","airbnb.com"
}
FREE_EMAIL = {
    "gmail.com","googlemail.com","outlook.com","hotmail.com","live.com","yahoo.com","icloud.com",
    "me.com","aol.com","proton.me","protonmail.com"
}
MULTIPART_SUFFIXES = (
    "co.uk","org.uk","me.uk","ltd.uk","plc.uk","net.uk","com.au","net.au","org.au",
    "com.br","com.mx","co.nz","net.nz","org.nz","co.za","com.pt","com.es","com.tr",
    "co.jp","com.sg","com.hk","com.my"
)
COUNTRY_DISPLAY = {
    "US":"USA","CA":"Canada","MX":"Mexico","GB":"United Kingdom","IE":"Ireland","FR":"France",
    "ES":"Spain","PT":"Portugal","IT":"Italy","GR":"Greece","DE":"Germany","AT":"Austria",
    "CH":"Switzerland","NL":"Netherlands","BE":"Belgium","LU":"Luxembourg","DK":"Denmark",
    "NO":"Norway","SE":"Sweden","FI":"Finland","IS":"Iceland","PL":"Poland","CZ":"Czechia",
    "SK":"Slovakia","HU":"Hungary","SI":"Slovenia","HR":"Croatia","ME":"Montenegro",
    "AL":"Albania","MT":"Malta","CY":"Cyprus","RO":"Romania","BG":"Bulgaria","RS":"Serbia",
    "BA":"Bosnia and Herzegovina","MK":"North Macedonia","EE":"Estonia","LV":"Latvia","LT":"Lithuania",
    "TR":"Turkey","GE":"Georgia","AE":"United Arab Emirates","SA":"Saudi Arabia","QA":"Qatar",
    "BH":"Bahrain","OM":"Oman","JO":"Jordan","MA":"Morocco","TN":"Tunisia","EG":"Egypt",
    "AU":"Australia","NZ":"New Zealand","FJ":"Fiji","JP":"Japan","KR":"South Korea","TH":"Thailand",
    "VN":"Vietnam","MY":"Malaysia","SG":"Singapore","ID":"Indonesia","PH":"Philippines","LK":"Sri Lanka",
    "MV":"Maldives","IN":"India","CN":"China","HK":"Hong Kong","TW":"Taiwan","ZA":"South Africa",
    "MU":"Mauritius","SC":"Seychelles","KE":"Kenya","TZ":"Tanzania","NA":"Namibia","MZ":"Mozambique",
    "MG":"Madagascar","BR":"Brazil","AR":"Argentina","CL":"Chile","UY":"Uruguay","CO":"Colombia",
    "PE":"Peru","EC":"Ecuador","BS":"Bahamas","JM":"Jamaica","DO":"Dominican Republic","KY":"Cayman Islands",
    "TC":"Turks and Caicos Islands","BB":"Barbados","AW":"Aruba","CW":"Curaçao","LC":"Saint Lucia",
    "AG":"Antigua and Barbuda","GD":"Grenada","BZ":"Belize","CR":"Costa Rica","PA":"Panama","GT":"Guatemala",
    "HN":"Honduras","NI":"Nicaragua","SV":"El Salvador","PR":"Puerto Rico","VI":"US Virgin Islands"
}


def norm(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def first(value) -> str:
    return norm(value[0]) if isinstance(value, (list, tuple)) and value else norm(value)


def host(url: str) -> str:
    try:
        h = (urlparse(url).hostname or "").lower().strip(".")
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""


def root_host(value: str) -> str:
    h = (value or "").lower().strip(".")
    if not h:
        return ""
    for suffix in MULTIPART_SUFFIXES:
        if h == suffix:
            return h
        if h.endswith("." + suffix):
            parts = h.split(".")
            return ".".join(parts[-3:])
    parts = h.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else h


def email_domain(email: str) -> str:
    e = norm(email).lower().strip("<>[](){}.,;:\"'")
    return e.rsplit("@", 1)[1] if "@" in e else ""


def valid_email(email: str) -> bool:
    e = norm(email).lower().strip("<>[](){}.,;:\"'")
    if not re.fullmatch(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", e):
        return False
    d = email_domain(e)
    if d in BAD_EMAIL_DOMAINS or any(d.endswith("." + x) for x in BAD_EMAIL_DOMAINS):
        return False
    if any(x in e for x in ("example@", "test@", "noreply@", "no-reply@", "donotreply@")):
        return False
    return True


def usable_existing_email(emails, website: str) -> bool:
    if not isinstance(emails, (list, tuple)):
        return False
    wd = root_host(host(website))
    for email in emails:
        e = norm(email)
        if not valid_email(e):
            continue
        ed = root_host(email_domain(e))
        if ed == wd or ed in FREE_EMAIL:
            return True
    return False


def brand_name(value) -> str:
    if not isinstance(value, dict):
        return ""
    names = value.get("names")
    return norm(names.get("primary")) if isinstance(names, dict) else norm(value.get("name"))


def addr(value) -> dict:
    if not isinstance(value, (list, tuple)) or not value or not isinstance(value[0], dict):
        return {"city":"", "state":"", "street":"", "country":""}
    a = value[0]
    lines = a.get("address_lines")
    street = ", ".join(norm(x) for x in lines if norm(x)) if isinstance(lines, (list, tuple)) else norm(lines or a.get("freeform"))
    return {
        "city": norm(a.get("locality") or a.get("city")),
        "state": norm(a.get("region") or a.get("state")),
        "street": street,
        "country": norm(a.get("country")).upper(),
    }


def display_country(code: str) -> str:
    c = norm(code).upper()
    return COUNTRY_DISPLAY.get(c, c)


def hits(text: str, phrases) -> int:
    low = (text or "").lower()
    return sum(1 for p in phrases if p in low)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bbox", required=True)
    ap.add_argument("--country", default="")
    ap.add_argument("--region", default="")
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--max-rows", type=int, default=80000)
    ap.add_argument("--release", default="2026-06-17.0")
    a = ap.parse_args()

    t0 = time.time()
    minlon, minlat, maxlon, maxlat = map(float, a.bbox.split(","))
    out = Path(a.outdir)
    out.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2';")
    path = f"s3://overturemaps-us-west-2/release/{a.release}/theme=places/type=place/*"
    q = f"""
      SELECT id,names.primary AS name,basic_category,taxonomy.primary AS taxonomy_primary,
             categories.primary AS category_primary,websites,emails,phones,brand,addresses,
             confidence,operating_status
      FROM read_parquet('{path}',hive_partitioning=1)
      WHERE bbox.xmin>={minlon} AND bbox.xmax<={maxlon}
        AND bbox.ymin>={minlat} AND bbox.ymax<={maxlat}
        AND (operating_status IS NULL OR operating_status='open')
        AND websites IS NOT NULL AND len(websites)>0
        AND (
          COALESCE(list_contains(taxonomy.hierarchy,'lodging'),false)
          OR COALESCE(basic_category,'') IN ('hotel','resort','lodging','vacation_rental','property_management','real_estate_agency')
          OR COALESCE(categories.primary,'') ILIKE '%hotel%'
          OR COALESCE(categories.primary,'') ILIKE '%resort%'
          OR COALESCE(categories.primary,'') ILIKE '%vacation%rental%'
          OR COALESCE(categories.primary,'') ILIKE '%holiday%rental%'
          OR COALESCE(categories.primary,'') ILIKE '%holiday%home%'
          OR COALESCE(categories.primary,'') ILIKE '%property%management%'
          OR COALESCE(categories.primary,'') ILIKE '%serviced%apartment%'
          OR COALESCE(categories.primary,'') ILIKE '%aparthotel%'
          OR COALESCE(categories.primary,'') ILIKE '%villa%'
          OR COALESCE(categories.primary,'') ILIKE '%chalet%'
          OR COALESCE(categories.primary,'') ILIKE '%cabin%rental%'
          OR COALESCE(names.primary,'') ILIKE '%vacation%rental%'
          OR COALESCE(names.primary,'') ILIKE '%holiday%rental%'
          OR COALESCE(names.primary,'') ILIKE '%holiday%home%'
          OR COALESCE(names.primary,'') ILIKE '%property%management%'
          OR COALESCE(names.primary,'') ILIKE '%short%term%rental%'
          OR COALESCE(names.primary,'') ILIKE '%serviced%apartment%'
          OR COALESCE(names.primary,'') ILIKE '%villa%rental%'
          OR COALESCE(names.primary,'') ILIKE '%chalet%rental%'
          OR COALESCE(names.primary,'') ILIKE '%cabin%rental%'
          OR COALESCE(names.primary,'') ILIKE '%boutique%hotel%'
          OR COALESCE(names.primary,'') ILIKE '%luxury%stay%'
        )
      LIMIT {int(a.max_rows)}
    """
    cur = con.execute(q)
    cols = [d[0] for d in cur.description]
    raw = [dict(zip(cols, row)) for row in cur.fetchall()]

    rows = []
    seen = set()
    rejected = {"existing_usable_email":0, "hard_reject":0, "weak_fit":0, "duplicate_domain":0, "missing_identity":0}
    for x in raw:
        name = norm(x.get("name"))
        site = first(x.get("websites"))
        phone = first(x.get("phones"))
        dom = root_host(host(site))
        cat = norm(x.get("taxonomy_primary") or x.get("category_primary") or x.get("basic_category"))
        brand = brand_name(x.get("brand"))
        ad = addr(x.get("addresses"))
        if not name or not dom:
            rejected["missing_identity"] += 1
            continue
        if usable_existing_email(x.get("emails"), site):
            rejected["existing_usable_email"] += 1
            continue
        identity = norm(" ".join([name, brand, cat])).lower()
        if hits(identity, HARD_REJECT):
            rejected["hard_reject"] += 1
            continue
        oh = hits(identity, OPERATOR)
        ph = hits(identity, PREMIUM)
        op_score = min(100, (48 if oh else 0) + (12 * min(3, oh)) + (15 if any(k in name.lower() for k in ("rentals","property management","vacation","holiday","villas","homes","stays")) else 0))
        p_score = min(100, (20 if "resort" in cat.lower() else 0) + (10 if "hotel" in cat.lower() else 0) + 12 * min(4, ph))
        operatorish = op_score >= 48 or any(k in cat.lower() for k in ("vacation","holiday rental","property management","serviced apartment"))
        propertyish = p_score >= 34
        if not (operatorish or propertyish):
            rejected["weak_fit"] += 1
            continue
        if dom in seen:
            rejected["duplicate_domain"] += 1
            continue
        seen.add(dom)
        tier = "A" if operatorish and (op_score >= 60 or p_score >= 24) else ("A" if p_score >= 48 else "B")
        oid = norm(x.get("id"))
        country = display_country(ad.get("country")) if norm(a.country).upper() == "AUTO" else norm(a.country)
        rows.append({
            "source":"Overture Places V6 site-recovery",
            "overture_id":oid,
            "country":country,
            "region":a.region,
            "name":name,
            "category":cat,
            "brand":brand,
            "website":site,
            "domain":dom,
            "public_email":"",
            "email_domain":"",
            "email_domain_match":"",
            "public_phone":phone,
            "city":ad["city"],
            "state":ad["state"],
            "street":ad["street"],
            "confidence":norm(x.get("confidence")),
            "operator_score":str(op_score),
            "premium_score":str(p_score),
            "fit_tier":tier,
            "source_url":f"https://explore.overturemaps.org/#id={oid}" if oid else "",
            "notes":"Public Overture website; no usable Overture email. First-party public contact crawl required; no inference."
        })

    rows.sort(key=lambda r: (r["fit_tier"] == "A", int(r["operator_score"]), int(r["premium_score"])), reverse=True)
    candidate_path = out / "v6_recovery_candidates.csv"
    with candidate_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    summary = {
        "release": a.release,
        "country": a.country,
        "region": a.region,
        "bbox": a.bbox,
        "raw_site_rows": len(raw),
        "raw_site_email_rows": len(raw),
        "recovery_candidates": len(rows),
        "fast_ready": 0,
        "tier_a_candidates": sum(r["fit_tier"] == "A" for r in rows),
        "tier_b_candidates": sum(r["fit_tier"] == "B" for r in rows),
        "rejects": rejected,
        "elapsed_seconds": round(time.time() - t0, 2)
    }
    (out / "v6_recovery_discovery_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    (out / "v6_fast_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
