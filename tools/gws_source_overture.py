#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import re
import shutil
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import duckdb

RELEASE = "2026-06-17.0"
BBOX = (50.75, 4.20, 50.95, 4.55)  # south, west, north, east
BRUSSELS_POSTCODES = {
    "1000", "1020", "1030", "1040", "1050", "1060", "1070", "1080", "1081", "1082",
    "1083", "1090", "1120", "1130", "1140", "1150", "1160", "1170", "1180", "1190", "1200", "1210",
}
PLATFORM_DOMAINS = (
    "facebook.com", "instagram.com", "tiktok.com", "linkedin.com", "youtube.com",
    "treatwell.", "planity.com", "fresha.com", "salonkee.", "nearcut.", "booking.com",
    "tripadvisor.", "yelp.", "google.", "maps.apple.", "waze.com", "ubereats.com",
    "deliveroo.", "takeaway.com", "pagesdor.be", "goudengids.be", "cylex.", "opendi.",
)
CHAIN_WORDS = (
    "carrefour", "delhaize", "lidl", "aldi", "action", "kruidvat", "ici paris", "di beauty",
    "basic fit", "orange", "proximus", "base shop", "telenet", "mediamarkt", "quick",
    "mcdonald", "burger king", "starbucks", "pizza hut", "domino", "panos", "exki",
    "bnp paribas", "belfius", "ing", "kbc", "crelan",
)


def iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def txt(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()


def norm(v: Any) -> str:
    raw = unicodedata.normalize("NFKD", txt(v)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", raw)).strip()


def load_json(path: str | Path, default: Any) -> Any:
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def dump_json(path: str | Path, value: Any) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_jsonl(path: Path) -> list[dict]:
    rows = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: list[dict]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            line = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            fh.write(line + "\n")
            h.update((line + "\n").encode("utf-8"))
    return h.hexdigest()


def host(url: Any) -> str:
    value = txt(url)
    if not value:
        return ""
    if "://" not in value:
        value = "https://" + value
    try:
        h = (urlparse(value).hostname or "").lower().strip(".")
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""


def owned_site(websites: Any) -> str:
    vals = websites if isinstance(websites, (list, tuple)) else ([websites] if websites else [])
    for u in vals:
        h = host(u)
        if h and not any(p in h for p in PLATFORM_DOMAINS):
            return txt(u)
    return ""


def first(values: Any) -> str:
    if isinstance(values, (list, tuple)):
        return txt(values[0]) if values else ""
    return txt(values)


def brand_name(v: Any) -> str:
    if not isinstance(v, dict):
        return ""
    names = v.get("names") or {}
    if isinstance(names, dict):
        return txt(names.get("primary"))
    return ""


def category_type(basic: Any, primary: Any) -> str:
    c = norm(primary or basic).replace(" ", "_")
    b = norm(basic).replace(" ", "_")
    hay = f"{b} {c}"
    rules = [
        (("hair_salon", "hairdresser", "barber"), "Hairdresser"),
        (("beauty_salon", "beauty_shop", "nail_salon", "manicure", "pedicure", "aesthetic"), "Beauty salon"),
        (("bakery",), "Bakery"),
        (("butcher", "meat_shop"), "Butcher"),
        (("dry_clean",), "Dry cleaning"),
        (("laundry", "laundromat"), "Laundry"),
        (("tailor", "alteration"), "Tailor"),
        (("shoe_repair", "cobbler"), "Shoe repair"),
        (("florist", "flower_shop"), "Florist"),
        (("garden_center", "garden_centre"), "Garden center"),
        (("pet_groom",), "Pet grooming"),
        (("optician", "eyewear_and_optician", "eyewear"), "Optician"),
        (("mobile_phone", "cell_phone", "phone_repair"), "Mobile phones"),
        (("copy_shop", "copyshop", "printing_service", "printer"), "Printing"),
        (("photo_studio", "photographer", "photography_service"), "Photo studio"),
        (("car_repair", "auto_repair", "automotive_repair", "auto_body"), "Garage"),
        (("tire", "tyre"), "Tyres"),
        (("locksmith",), "Locksmith"),
        (("watch_repair", "watchmaker"), "Watch repair"),
        (("jewelry_repair", "jewellery_repair"), "Jewelry repair"),
        (("plumber", "plumbing"), "Plumber"),
        (("electrician", "electrical_service"), "Electrician"),
        (("heating", "hvac"), "Heating"),
        (("massage", "day_spa", "wellness"), "Massage"),
        (("podiat", "podolog"), "Podiatry"),
    ]
    for needles, typ in rules:
        if any(n in hay for n in needles):
            return typ
    return ""


def address_parts(addresses: Any) -> tuple[str, str, str]:
    vals = addresses if isinstance(addresses, (list, tuple)) else []
    for a in vals:
        if not isinstance(a, dict):
            continue
        pc = re.sub(r"\s+", "", txt(a.get("postcode")))
        if pc not in BRUSSELS_POSTCODES:
            continue
        freeform = txt(a.get("freeform"))
        locality = txt(a.get("locality"))
        return pc, freeform, locality
    return "", "", ""


def query_places(threads: int) -> tuple[list[dict], dict]:
    south, west, north, east = BBOX
    path = f"s3://overturemaps-us-west-2/release/{RELEASE}/theme=places/type=place/*"
    con = duckdb.connect()
    con.execute(f"PRAGMA threads={max(1, int(threads))}")
    con.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2';")
    q = f"""
      SELECT id, names.primary AS name, basic_category, categories.primary AS category_primary,
             websites, socials, emails, phones, brand, addresses, confidence, operating_status,
             (bbox.xmin+bbox.xmax)/2.0 AS longitude,
             (bbox.ymin+bbox.ymax)/2.0 AS latitude
      FROM read_parquet('{path}', hive_partitioning=1)
      WHERE bbox.xmax >= {west} AND bbox.xmin <= {east}
        AND bbox.ymax >= {south} AND bbox.ymin <= {north}
        AND (operating_status IS NULL OR operating_status='open')
        AND names.primary IS NOT NULL
    """
    cur = con.execute(q)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    return rows, {"release": RELEASE, "raw_places_bbox": len(rows), "threads": threads}


def normalize_place(p: dict) -> dict | None:
    name = txt(p.get("name"))
    typ = category_type(p.get("basic_category"), p.get("category_primary"))
    pc, freeform, locality = address_parts(p.get("addresses"))
    if not name or not typ or not pc:
        return None
    site = owned_site(p.get("websites"))
    if site:
        return None
    brand = brand_name(p.get("brand"))
    chain_hay = norm(f"{name} {brand}")
    if any(norm(c) in chain_hay for c in CHAIN_WORDS):
        return None
    lat = float(p.get("latitude"))
    lon = float(p.get("longitude"))
    oid = "overture:" + txt(p.get("id"))
    return {
        "objectid": oid,
        "name_en": name,
        "type_en": typ,
        "category_en": "Day-to-day products" if typ in {"Bakery", "Butcher"} else "Services",
        "address_en": freeform or locality,
        "postalcode": pc,
        "geo_point_2d": {"lat": lat, "lon": lon},
        "google_maps": f"https://www.google.com/maps/search/?api=1&query={lat}%2C{lon}",
        "source": "Overture Maps direct",
        "source_ref": oid,
        "source_phone": first(p.get("phones")),
        "source_email": first(p.get("emails")),
        "source_socials": p.get("socials") or [],
        "source_website": "",
        "source_confidence": p.get("confidence"),
        "source_category": txt(p.get("category_primary") or p.get("basic_category")),
    }


def prepare(args: argparse.Namespace) -> int:
    plan_dir = Path(args.plan_dir)
    base_rows = read_jsonl(Path(args.input_candidates))
    try:
        places, stats = query_places(args.threads)
        normalized = [normalize_place(p) for p in places]
        direct = [r for r in normalized if r is not None]
        local_dedup = {}
        for row in direct:
            key = (norm(row.get("name_en")), txt(row.get("postalcode")), norm(row.get("address_en")))
            local_dedup.setdefault(key, row)
        direct = list(local_dedup.values())
        direct_path = plan_dir / "overture_direct_current.jsonl"
        sha = write_jsonl(direct_path, direct)
        status = {"status": "fresh", "sha256": sha, "materialized": len(direct), **stats, "last_fetched": iso_now()}
    except Exception as exc:
        direct = []
        status = {"status": "failed_nonfatal", "error": f"{type(exc).__name__}: {exc}", "materialized": 0, "release": RELEASE}

    base_keys = {(norm(r.get("name_en") or r.get("name_fr") or r.get("name_nl")), txt(r.get("postalcode"))) for r in base_rows}
    unique = []
    dupes = 0
    for row in direct:
        key = (norm(row.get("name_en")), txt(row.get("postalcode")))
        if key in base_keys:
            dupes += 1
            continue
        unique.append(row)
    merged = base_rows + unique
    write_jsonl(Path(args.out_candidates), merged)
    status.update({"base_candidates": len(base_rows), "overture_unique_after_cross_dedupe": len(unique), "cross_source_duplicates": dupes, "merged_candidates": len(merged)})
    dump_json(plan_dir / "overture_direct_summary.json", status)
    print(json.dumps(status, indent=2, ensure_ascii=False))
    return 0


def persist(args: argparse.Namespace) -> int:
    plan_dir = Path(args.plan_dir)
    summary = load_json(plan_dir / "overture_direct_summary.json", {})
    if not summary:
        return 0
    state_path = Path("state/gws_source_state.json")
    state = load_json(state_path, {"schema_version": 1, "sources": {}})
    prior = state.setdefault("sources", {}).get("overture_direct", {})
    sha = summary.get("sha256") or prior.get("sha256")
    changed = bool(sha and sha != prior.get("sha256"))
    persisted = prior.get("persisted_snapshot")
    source_path = plan_dir / "overture_direct_current.jsonl"
    if source_path.exists() and sha and changed:
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = Path("data/gws/source") / f"overture_direct_{stamp}_{str(sha)[:12]}.jsonl.gz"
        dest.parent.mkdir(parents=True, exist_ok=True)
        with source_path.open("rb") as src, gzip.open(dest, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst)
        persisted = str(dest)
    state["sources"]["overture_direct"] = {
        "status": summary.get("status"), "release": summary.get("release", RELEASE), "sha256": sha,
        "last_seen": iso_now(), "last_fetched": summary.get("last_fetched") or prior.get("last_fetched"),
        "last_changed": iso_now() if changed else prior.get("last_changed"),
        "materialized": summary.get("materialized", 0),
        "overture_unique_after_cross_dedupe": summary.get("overture_unique_after_cross_dedupe", 0),
        "persisted_snapshot": persisted, "error": summary.get("error", ""),
    }
    dump_json(state_path, state)
    print(json.dumps({"status": "persisted", "changed": changed, "source": state["sources"]["overture_direct"]}, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--input-candidates", required=True)
    prep.add_argument("--out-candidates", required=True)
    prep.add_argument("--plan-dir", default="results/fleet_plan")
    prep.add_argument("--threads", type=int, default=4)
    per = sub.add_parser("persist")
    per.add_argument("--plan-dir", default="results/fleet_plan")
    args = ap.parse_args()
    return prepare(args) if args.command == "prepare" else persist(args)


if __name__ == "__main__":
    raise SystemExit(main())
