#!/usr/bin/env python3
"""Stream one planned AllThePlaces partition into the hospitality row contract.

Transport is HTTP Range against the official weekly output ZIP. GeoJSON members
are parsed incrementally; we never materialize the full archive or a whole large
FeatureCollection in memory.

This stage performs discovery/normalization only. It does not crawl candidate
websites. Rows are split into:
  - atp_fast_ready.csv: explicit public email + website, domain-compatible;
  - atp_recovery_candidates.csv: website, but no usable explicit ATP email.

Both outputs are intended to pass through canonical-domain prefilter before HTTP,
then the existing live verifier / first-party recovery crawler. No email patterns
are inferred and no authentication is attempted.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import re
import time
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import fsspec
import ijson

FIELDS = [
    "source","source_family","source_release","source_record_id","atp_id","atp_spider",
    "country","region","name","category","brand","operator","website","domain",
    "public_email","email_domain","email_domain_match","public_phone","city","state","street",
    "confidence","operator_score","premium_score","fit_tier","source_url","overture_id","notes",
    "instagram","facebook"
]
MULTIPART_SUFFIXES = (
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
OPERATOR = (
    "vacation rental","vacation rentals","vacation home","vacation homes","holiday rental","holiday rentals",
    "holiday home","holiday homes","holiday let","holiday lets","villa rental","villa rentals","villa management",
    "property management","rental management","short term rental","short-term rental","short stay","short-stay",
    "serviced apartment","serviced apartments","serviced accommodation","aparthotel","apartment hotel","luxury rentals",
    "managed homes","managed properties","condo rentals","cabin rentals","chalet rentals","beach rentals",
    "vacation property","self catering","self-catering","rental agency","holiday cottages","property manager",
    "vacation management","holiday accommodation","vacation accommodation","lodging management"
)
PREMIUM = (
    "luxury","boutique","villa","villas","resort","retreat","estate","residence","residences","beachfront",
    "oceanfront","waterfront","ski","chalet","penthouse","private island","collection","lodge","spa hotel",
    "country house","country resort","eco resort","glamping","holiday park"
)
HOSP_KEYS = (
    "hotel","resort","lodging","guest house","guest_house","bed and breakfast","bed_and_breakfast","b&b",
    "vacation rental","holiday rental","holiday home","serviced apartment","aparthotel","villa","chalet",
    "cottage","self catering","self-catering","rental accommodation","tourist accommodation","inn"
)
HARD_REJECT = (
    "hostel","backpacker","motel 6","super 8","econo lodge","econolodge","rodeway inn","quality inn",
    "comfort inn","days inn","red roof","budget inn","student housing","senior living","assisted living",
    "campground","camp site","camp_site","rv park","rv resort","timeshare sales","wedding planner"
)
COUNTRY_DISPLAY = {
    "US":"USA","CA":"Canada","MX":"Mexico","GB":"United Kingdom","IE":"Ireland","FR":"France","ES":"Spain",
    "PT":"Portugal","IT":"Italy","GR":"Greece","DE":"Germany","AT":"Austria","CH":"Switzerland",
    "NL":"Netherlands","BE":"Belgium","LU":"Luxembourg","DK":"Denmark","NO":"Norway","SE":"Sweden",
    "FI":"Finland","IS":"Iceland","PL":"Poland","CZ":"Czechia","SK":"Slovakia","HU":"Hungary",
    "SI":"Slovenia","HR":"Croatia","ME":"Montenegro","AL":"Albania","MT":"Malta","CY":"Cyprus",
    "RO":"Romania","BG":"Bulgaria","RS":"Serbia","BA":"Bosnia and Herzegovina","MK":"North Macedonia",
    "EE":"Estonia","LV":"Latvia","LT":"Lithuania","TR":"Turkey","GE":"Georgia","AE":"United Arab Emirates",
    "SA":"Saudi Arabia","QA":"Qatar","BH":"Bahrain","OM":"Oman","JO":"Jordan","MA":"Morocco","TN":"Tunisia",
    "EG":"Egypt","AU":"Australia","NZ":"New Zealand","FJ":"Fiji","JP":"Japan","KR":"South Korea",
    "TH":"Thailand","VN":"Vietnam","MY":"Malaysia","SG":"Singapore","ID":"Indonesia","PH":"Philippines",
    "LK":"Sri Lanka","MV":"Maldives","IN":"India","CN":"China","HK":"Hong Kong","TW":"Taiwan",
    "ZA":"South Africa","MU":"Mauritius","SC":"Seychelles","KE":"Kenya","TZ":"Tanzania","NA":"Namibia",
    "MZ":"Mozambique","MG":"Madagascar","BR":"Brazil","AR":"Argentina","CL":"Chile","UY":"Uruguay",
    "CO":"Colombia","PE":"Peru","EC":"Ecuador","BS":"Bahamas","JM":"Jamaica","DO":"Dominican Republic",
    "KY":"Cayman Islands","TC":"Turks and Caicos Islands","BB":"Barbados","AW":"Aruba","CW":"Curaçao",
    "LC":"Saint Lucia","AG":"Antigua and Barbuda","GD":"Grenada","BZ":"Belize","CR":"Costa Rica",
    "PA":"Panama","GT":"Guatemala","HN":"Honduras","NI":"Nicaragua","SV":"El Salvador","PR":"Puerto Rico",
    "VI":"US Virgin Islands"
}


def norm(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return norm(v[0]) if v else ""
    if isinstance(v, dict):
        return ""
    return re.sub(r"\s+", " ", str(v)).strip()


def root_host(value: str) -> str:
    h = (value or "").lower().strip(".")
    if h.startswith("www."):
        h = h[4:]
    if not h:
        return ""
    for suffix in MULTIPART_SUFFIXES:
        if h == suffix:
            return h
        if h.endswith("." + suffix):
            p = h.split(".")
            return ".".join(p[-3:])
    p = h.split(".")
    return ".".join(p[-2:]) if len(p) >= 2 else h


def host(url: str) -> str:
    try:
        h = (urlparse(url).hostname or "").lower().strip(".")
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""


def normalize_url(v) -> str:
    u = norm(v)
    if not u:
        return ""
    if u.startswith("//"):
        u = "https:" + u
    if not re.match(r"^https?://", u, re.I):
        if re.match(r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}(/|$)", u):
            u = "https://" + u
        else:
            return ""
    try:
        p = urlparse(u)
        if p.scheme not in ("http", "https") or not p.hostname:
            return ""
        return u
    except Exception:
        return ""


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


def split_emails(v):
    if isinstance(v, (list, tuple)):
        parts = [norm(x) for x in v]
    else:
        parts = re.split(r"[\s,;]+", norm(v))
    out = []
    for x in parts:
        e = x.lower().strip("<>[](){}.,;:\"'")
        if valid_email(e) and e not in out:
            out.append(e)
    return out


def prop(props: dict, *keys):
    for k in keys:
        if k in props and props[k] not in (None, "", [], {}):
            return props[k]
    return ""


def flatten_selected(props: dict) -> str:
    vals = []
    for k in (
        "name","brand","operator","category","categories","tourism","amenity","shop","description",
        "branch","@spider","website","contact:website"
    ):
        v = props.get(k)
        if isinstance(v, (str, int, float)):
            vals.append(str(v))
        elif isinstance(v, (list, tuple)):
            vals.extend(str(x) for x in v if isinstance(x, (str, int, float)))
    return " ".join(vals).lower()


def phrase_hits(text: str, phrases) -> int:
    return sum(1 for p in phrases if p in text)


def first_social(v, network: str) -> str:
    u = norm(v)
    if not u:
        return ""
    if u.startswith("http://") or u.startswith("https://"):
        return u
    handle = u.lstrip("@/")
    if network == "instagram" and re.fullmatch(r"[A-Za-z0-9._]{1,30}", handle):
        return f"https://www.instagram.com/{handle}/"
    if network == "facebook" and handle:
        return f"https://www.facebook.com/{handle}/"
    return ""


def display_country(v) -> str:
    c = norm(v).upper()
    return COUNTRY_DISPLAY.get(c, c)


def normalized_feature(feature: dict, archive_run: str, member: str):
    props = feature.get("properties") if isinstance(feature, dict) else None
    if not isinstance(props, dict):
        return None, "missing_properties"
    name = norm(prop(props, "name", "branch", "brand"))
    brand = norm(prop(props, "brand"))
    operator = norm(prop(props, "operator"))
    spider = norm(prop(props, "@spider", "spider")) or Path(member).stem
    source_uri = normalize_url(prop(props, "@source_uri", "source_uri", "source"))
    website = normalize_url(prop(props, "website", "contact:website", "url"))
    dom = root_host(host(website))
    if not name or not website or not dom:
        return None, "missing_name_or_website"

    text = flatten_selected(props)
    if phrase_hits(text, HARD_REJECT):
        return None, "hard_reject"
    op_hits = phrase_hits(text, OPERATOR)
    premium_hits = phrase_hits(text, PREMIUM)
    hosp_hits = phrase_hits(text, HOSP_KEYS)
    tourism = norm(prop(props, "tourism")).lower().replace("_", " ")
    explicit_hospitality = tourism in {
        "hotel","resort","guest house","apartment","chalet","bed and breakfast","inn","holiday apartment"
    }
    if not (explicit_hospitality or hosp_hits or op_hits):
        return None, "weak_fit"

    op_score = min(100, (48 if op_hits else 0) + 12 * min(3, op_hits) + (12 if operator else 0))
    premium_score = min(100, 12 * min(4, premium_hits) + (20 if "resort" in text else 0) + (10 if "hotel" in text else 0))
    operatorish = op_score >= 48
    propertyish = explicit_hospitality and premium_score >= 20 or premium_hits >= 2
    if not (operatorish or propertyish):
        return None, "weak_fit"

    emails = []
    for key in ("email", "contact:email"):
        emails.extend(split_emails(props.get(key)))
    emails = list(dict.fromkeys(emails))
    usable = []
    for e in emails:
        rd = root_host(email_domain(e))
        if rd == dom or rd in FREE_EMAIL:
            usable.append(e)
    best_email = usable[0] if usable else ""
    ed = root_host(email_domain(best_email)) if best_email else ""

    phone = norm(prop(props, "phone", "contact:phone"))
    city = norm(prop(props, "addr:city", "city", "locality"))
    state = norm(prop(props, "addr:state", "state", "region"))
    country = display_country(prop(props, "addr:country", "country"))
    street = norm(prop(props, "addr:full", "addr:street_address", "street_address", "address"))
    if not street:
        hn = norm(prop(props, "addr:housenumber"))
        sn = norm(prop(props, "addr:street"))
        street = " ".join(x for x in (hn, sn) if x)

    raw_id = norm(prop(props, "ref", "id", "@id")) or norm(feature.get("id") if isinstance(feature, dict) else "")
    stable_material = "|".join((archive_run, spider, raw_id, source_uri, name, website))
    source_record_id = "atp:" + hashlib.sha1(stable_material.encode("utf-8")).hexdigest()[:24]
    instagram = first_social(prop(props, "contact:instagram", "instagram"), "instagram")
    facebook = first_social(prop(props, "contact:facebook", "facebook"), "facebook")
    category = " / ".join(x for x in (norm(prop(props, "tourism")), norm(prop(props, "category")), norm(prop(props, "amenity"))) if x)
    tier = "A" if operatorish and (op_score >= 60 or premium_score >= 24) else ("A" if premium_score >= 48 else "B")

    row = {
        "source": "AllThePlaces weekly GeoJSON",
        "source_family": "alltheplaces",
        "source_release": archive_run,
        "source_record_id": source_record_id,
        "atp_id": raw_id,
        "atp_spider": spider,
        "country": country,
        "region": f"ATP::{spider}",
        "name": name,
        "category": category,
        "brand": brand,
        "operator": operator,
        "website": website,
        "domain": dom,
        "public_email": best_email,
        "email_domain": email_domain(best_email),
        "email_domain_match": "YES" if best_email and ed == dom else ("FREE_WEBMAIL" if best_email else ""),
        "public_phone": phone,
        "city": city,
        "state": state,
        "street": street,
        "confidence": "ATP_PUBLIC_SOURCE",
        "operator_score": str(op_score),
        "premium_score": str(premium_score),
        "fit_tier": tier,
        "source_url": source_uri,
        "overture_id": "",
        "notes": f"Public AllThePlaces record from spider {spider}; explicit website/contact fields only; no inference.",
        "instagram": instagram,
        "facebook": facebook,
    }
    return row, "fast" if best_email else "recovery"


def iter_features(stream, member_name: str):
    # ATP output is normally a GeoJSON FeatureCollection. Be tolerant of gzipped
    # members and newline-delimited JSON variants so one odd spider cannot kill a lane.
    raw = stream
    if member_name.lower().endswith(".gz"):
        raw = gzip.GzipFile(fileobj=stream)
    buffered = io.BufferedReader(raw) if not isinstance(raw, io.BufferedReader) else raw
    low = member_name.lower()
    if low.endswith((".ndjson", ".ndjson.gz")):
        text = io.TextIOWrapper(buffered, encoding="utf-8", errors="replace")
        for line in text:
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if isinstance(obj, dict):
                yield obj
        return
    try:
        yield from ijson.items(buffered, "features.item")
    except Exception:
        return


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True)
    ap.add_argument("--partition", type=int, required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--block-mb", type=int, default=4)
    ap.add_argument("--max-features", type=int, default=0, help="Canary-only global feature cap; 0 = complete partition")
    a = ap.parse_args()

    t0 = time.time()
    plan = json.loads(Path(a.plan).read_text(encoding="utf-8"))
    partitions = plan.get("partitions") or []
    if a.partition < 0 or a.partition >= len(partitions):
        raise SystemExit(f"invalid partition {a.partition}; plan has {len(partitions)}")
    part = partitions[a.partition]
    archive_url = str(plan.get("archive_url") or "")
    archive_run = str(plan.get("archive_run") or "")
    members = [x.get("name") for x in (part.get("members") or []) if x.get("name")]

    outdir = Path(a.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    fast = []
    recovery = []
    seen_domains = set()
    stats = {
        "partition": a.partition,
        "members_planned": len(members),
        "members_completed": 0,
        "features_seen": 0,
        "hospitality_rows": 0,
        "fast_rows": 0,
        "recovery_rows": 0,
        "duplicate_domains": 0,
        "rejects": {},
        "member_errors": [],
    }

    remote = fsspec.open(archive_url, "rb", block_size=max(1, a.block_mb) * 1024 * 1024, cache_type="readahead").open()
    try:
        if not remote.seekable():
            raise RuntimeError("ATP remote archive not seekable")
        with zipfile.ZipFile(remote) as z:
            for member in members:
                if a.max_features and stats["features_seen"] >= a.max_features:
                    break
                try:
                    with z.open(member, "r") as stream:
                        for feature in iter_features(stream, member):
                            stats["features_seen"] += 1
                            row, reason = normalized_feature(feature, archive_run, member)
                            if not row:
                                stats["rejects"][reason] = stats["rejects"].get(reason, 0) + 1
                            else:
                                d = row["domain"]
                                if d in seen_domains:
                                    stats["duplicate_domains"] += 1
                                else:
                                    seen_domains.add(d)
                                    stats["hospitality_rows"] += 1
                                    if reason == "fast":
                                        fast.append(row)
                                    else:
                                        recovery.append(row)
                            if a.max_features and stats["features_seen"] >= a.max_features:
                                break
                    stats["members_completed"] += 1
                except Exception as e:
                    stats["member_errors"].append({"member": member, "error": f"{type(e).__name__}: {e}"})
    finally:
        remote.close()

    fast.sort(key=lambda r: (r["fit_tier"] == "A", int(r["operator_score"]), int(r["premium_score"]), r["name"].lower()), reverse=True)
    recovery.sort(key=lambda r: (r["fit_tier"] == "A", int(r["operator_score"]), int(r["premium_score"]), r["name"].lower()), reverse=True)
    write_csv(outdir / "atp_fast_ready.csv", fast)
    write_csv(outdir / "atp_recovery_candidates.csv", recovery)
    stats["fast_rows"] = len(fast)
    stats["recovery_rows"] = len(recovery)
    stats["elapsed_seconds"] = round(time.time() - t0, 2)
    stats["archive_run"] = archive_run
    stats["archive_url"] = archive_url
    stats["complete_partition"] = not bool(a.max_features) and stats["members_completed"] == len(members) and not stats["member_errors"]
    (outdir / "atp_partition_summary.json").write_text(json.dumps(stats, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
