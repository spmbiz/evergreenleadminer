#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import re
import shutil
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# Brussels-Capital bounding box. Overpass is used only as a discovery feed;
# strict website verification remains downstream.
BBOX = (50.75, 4.20, 50.95, 4.55)  # south, west, north, east
OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
BRUSSELS_POSTCODES = {
    "1000", "1020", "1030", "1040", "1050", "1060", "1070", "1080", "1081", "1082",
    "1083", "1090", "1120", "1130", "1140", "1150", "1160", "1170", "1180", "1190",
    "1200", "1210",
}
CITY_TO_POSTCODE = {
    "uccle": "1180", "ukkel": "1180",
    "ixelles": "1050", "elsene": "1050",
    "saint gilles": "1060", "sint gillis": "1060",
    "forest": "1190", "vorst": "1190",
    "auderghem": "1160", "oudergem": "1160",
    "watermael boitsfort": "1170", "watermaal bosvoorde": "1170",
    "etterbeek": "1040",
    "anderlecht": "1070",
    "molenbeek saint jean": "1080", "sint jans molenbeek": "1080",
    "koekelberg": "1081",
    "berchem sainte agathe": "1082", "sint agatha berchem": "1082",
    "ganshoren": "1083",
    "jette": "1090",
    "schaerbeek": "1030", "schaarbeek": "1030",
    "evere": "1140",
    "woluwe saint pierre": "1150", "sint pieters woluwe": "1150",
    "woluwe saint lambert": "1200", "sint lambrechts woluwe": "1200",
    "saint josse ten noode": "1210", "sint joost ten node": "1210",
}
PLATFORM_DOMAINS = (
    "facebook.com", "instagram.com", "tiktok.com", "linkedin.com", "youtube.com",
    "treatwell.", "planity.com", "fresha.com", "salonkee.", "nearcut.", "booking.com",
    "tripadvisor.", "yelp.", "google.", "maps.apple.", "waze.com", "ubereats.com",
    "deliveroo.", "takeaway.com", "pagesdor.be", "goudengids.be", "cylex.", "opendi.",
)

SHOP_MAP = {
    "hairdresser": "Hairdresser", "beauty": "Beauty salon", "bakery": "Bakery", "butcher": "Butcher",
    "laundry": "Laundry", "dry_cleaning": "Dry cleaning", "tailor": "Tailor", "shoe_repair": "Shoe repair",
    "florist": "Florist", "garden_centre": "Garden center", "pet_grooming": "Pet grooming",
    "optician": "Optician", "mobile_phone": "Mobile phones", "copyshop": "Copy shop",
    "photo": "Photo studio", "car_repair": "Garage", "tyres": "Tyres", "massage": "Massage",
}
CRAFT_MAP = {
    "locksmith": "Locksmith", "shoemaker": "Shoe repair", "watchmaker": "Watch repair",
    "jeweller": "Jewelry repair", "photographer": "Photo studio", "printer": "Printing",
    "plumber": "Plumber", "electrician": "Electrician", "hvac": "Heating",
    "heating_engineer": "Heating", "car_repair": "Garage", "tailor": "Tailor",
}
HEALTHCARE_MAP = {"podiatrist": "Podiatry", "podiatry": "Podiatry"}


def iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


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


def txt(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def norm(value: Any) -> str:
    raw = unicodedata.normalize("NFKD", txt(value)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", raw)).strip()


def host(url: str) -> str:
    try:
        h = (urllib.parse.urlparse(url).hostname or "").lower().strip(".")
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""


def owned_site(tags: dict) -> str:
    for key in ("website", "contact:website", "url"):
        value = txt(tags.get(key))
        if not value:
            continue
        h = host(value if "://" in value else "https://" + value)
        if h and not any(p in h for p in PLATFORM_DOMAINS):
            return value
    return ""


def postcode(tags: dict) -> str:
    pc = re.sub(r"\D", "", txt(tags.get("addr:postcode")))[:4]
    if pc in BRUSSELS_POSTCODES:
        return pc
    # Infer only from an unambiguous commune name. Generic Bruxelles/Brussel is deliberately ignored.
    for key in ("addr:city", "addr:suburb", "addr:municipality"):
        city = norm(tags.get(key))
        if city in CITY_TO_POSTCODE:
            return CITY_TO_POSTCODE[city]
    return ""


def target_type(tags: dict) -> str:
    shop = norm(tags.get("shop")).replace(" ", "_")
    craft = norm(tags.get("craft")).replace(" ", "_")
    healthcare = norm(tags.get("healthcare")).replace(" ", "_")
    amenity = norm(tags.get("amenity")).replace(" ", "_")
    if shop in SHOP_MAP:
        return SHOP_MAP[shop]
    if craft in CRAFT_MAP:
        return CRAFT_MAP[craft]
    if healthcare in HEALTHCARE_MAP:
        return HEALTHCARE_MAP[healthcare]
    if amenity == "veterinary" and norm(tags.get("vending")) == "":
        return "Pet grooming" if norm(tags.get("service")) in {"grooming", "pet_grooming"} else ""
    return ""


def coords(element: dict) -> tuple[float | None, float | None]:
    if element.get("lat") is not None and element.get("lon") is not None:
        return float(element["lat"]), float(element["lon"])
    center = element.get("center") or {}
    if center.get("lat") is not None and center.get("lon") is not None:
        return float(center["lat"]), float(center["lon"])
    return None, None


def address(tags: dict) -> str:
    street = txt(tags.get("addr:street"))
    number = txt(tags.get("addr:housenumber"))
    place = txt(tags.get("addr:place"))
    return txt(" ".join(x for x in (street or place, number) if x))


def social_values(tags: dict) -> list[str]:
    vals = []
    for key in ("facebook", "contact:facebook", "instagram", "contact:instagram", "twitter", "contact:twitter"):
        value = txt(tags.get(key))
        if value:
            vals.append(value)
    return vals


def normalize_element(element: dict) -> dict | None:
    tags = element.get("tags") or {}
    name = txt(tags.get("name"))
    typ = target_type(tags)
    pc = postcode(tags)
    lat, lon = coords(element)
    if not name or not typ or not pc or lat is None or lon is None:
        return None
    # Cheap screen: OSM already explicitly advertises a real owned site, so it cannot be a strict no-site lead.
    site = owned_site(tags)
    if site:
        return None
    osm_id = f"osm:{element.get('type')}:{element.get('id')}"
    return {
        "objectid": osm_id,
        "name_en": name,
        "type_en": typ,
        "category_en": "Services" if typ not in {"Bakery", "Butcher"} else "Day-to-day products",
        "address_en": address(tags),
        "postalcode": pc,
        "geo_point_2d": {"lat": lat, "lon": lon},
        "google_maps": f"https://www.google.com/maps/search/?api=1&query={lat}%2C{lon}",
        "source": "OpenStreetMap",
        "source_ref": osm_id,
        "source_phone": txt(tags.get("contact:phone") or tags.get("phone")),
        "source_email": txt(tags.get("contact:email") or tags.get("email")),
        "source_socials": social_values(tags),
        "source_website": "",
    }


def fetch_overpass() -> tuple[list[dict], dict]:
    s, w, n, e = BBOX
    query = f'''[out:json][timeout:120];(
      nwr["shop"]({s},{w},{n},{e});
      nwr["craft"]({s},{w},{n},{e});
      nwr["healthcare"]({s},{w},{n},{e});
    );out center tags;'''
    body = urllib.parse.urlencode({"data": query}).encode("utf-8")
    errors = []
    started = time.time()
    for endpoint in OVERPASS_ENDPOINTS:
        for attempt in range(1, 4):
            try:
                req = urllib.request.Request(endpoint, data=body, headers={
                    "User-Agent": "GWS-Brussels-Fleet/2.0 (OSM discovery)",
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                })
                with urllib.request.urlopen(req, timeout=150) as response:
                    payload = json.load(response)
                return payload.get("elements", []), {
                    "endpoint": endpoint, "attempt": attempt, "elapsed_seconds": round(time.time() - started, 3),
                    "errors": errors,
                }
            except Exception as exc:
                errors.append(f"{endpoint}:attempt={attempt}:{type(exc).__name__}:{exc}")
                time.sleep(min(8, attempt * 2))
    raise RuntimeError("Overpass discovery failed: " + " | ".join(errors[-6:]))


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


def restore_cached_osm(plan_dir: Path, state: dict, max_age_hours: float) -> tuple[list[dict], dict] | None:
    prior = state.get("sources", {}).get("osm_brussels", {})
    persisted = txt(prior.get("persisted_snapshot"))
    fetched_at = txt(prior.get("last_fetched"))
    if not persisted or not fetched_at:
        return None
    try:
        fetched = dt.datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
        age = (dt.datetime.now(dt.timezone.utc) - fetched).total_seconds() / 3600.0
    except Exception:
        return None
    if age > max_age_hours or not Path(persisted).exists():
        return None
    rows = []
    try:
        with gzip.open(persisted, "rt", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    rows.append(json.loads(line))
    except Exception:
        return None
    path = plan_dir / "osm_brussels_current.jsonl"
    sha = write_jsonl(path, rows)
    return rows, {"status": "cache", "sha256": sha, "materialized": len(rows), "last_fetched": fetched_at}


def prepare(args: argparse.Namespace) -> int:
    plan_dir = Path(args.plan_dir)
    plan_dir.mkdir(parents=True, exist_ok=True)
    hub_path = Path(args.hub_snapshot)
    hub_rows = read_jsonl(hub_path)
    state = load_json("state/gws_source_state.json", {"schema_version": 1, "sources": {}})

    source_status: dict[str, Any] = {}
    osm_rows: list[dict] = []
    cached = restore_cached_osm(plan_dir, state, args.max_age_hours)
    if cached:
        osm_rows, source_status = cached
    else:
        try:
            elements, transport = fetch_overpass()
            normalized = [normalize_element(e) for e in elements]
            osm_rows = [r for r in normalized if r is not None]
            # Stable source-local dedupe.
            dedup = {}
            for row in osm_rows:
                key = (norm(row.get("name_en")), txt(row.get("postalcode")), norm(row.get("address_en")))
                dedup.setdefault(key, row)
            osm_rows = list(dedup.values())
            osm_path = plan_dir / "osm_brussels_current.jsonl"
            sha = write_jsonl(osm_path, osm_rows)
            source_status = {
                "status": "fresh", "sha256": sha, "raw_elements": len(elements), "materialized": len(osm_rows),
                "last_fetched": iso_now(), **transport,
            }
        except Exception as exc:
            source_status = {"status": "failed_nonfatal", "error": f"{type(exc).__name__}: {exc}", "materialized": 0}
            osm_rows = []

    # Cross-source cheap dedupe before expensive Overture verification.
    hub_keys = {(norm(r.get("name_en") or r.get("name_fr") or r.get("name_nl")), txt(r.get("postalcode"))) for r in hub_rows}
    osm_unique = []
    cross_dupes = 0
    for row in osm_rows:
        key = (norm(row.get("name_en")), txt(row.get("postalcode")))
        if key in hub_keys:
            cross_dupes += 1
            continue
        osm_unique.append(row)

    merged = hub_rows + osm_unique
    out_candidates = Path(args.out_candidates)
    write_jsonl(out_candidates, merged)
    source_status.update({
        "hub_rows": len(hub_rows), "osm_unique_after_hub_dedupe": len(osm_unique),
        "cross_source_duplicates": cross_dupes, "merged_candidates": len(merged),
    })
    dump_json(plan_dir / "osm_snapshot_summary.json", source_status)
    print(json.dumps(source_status, indent=2, ensure_ascii=False))
    return 0


def persist(args: argparse.Namespace) -> int:
    plan_dir = Path(args.plan_dir)
    summary = load_json(plan_dir / "osm_snapshot_summary.json", {})
    source_path = plan_dir / "osm_brussels_current.jsonl"
    if not summary:
        print(json.dumps({"status": "noop", "reason": "no_osm_summary"}))
        return 0

    state_path = Path("state/gws_source_state.json")
    state = load_json(state_path, {"schema_version": 1, "sources": {}})
    sources = state.setdefault("sources", {})
    prior = sources.get("osm_brussels", {})
    sha = summary.get("sha256") or prior.get("sha256")
    changed = bool(sha and sha != prior.get("sha256"))
    persisted = prior.get("persisted_snapshot")

    if source_path.exists() and sha and changed:
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = Path("data/gws/source") / f"osm_brussels_{stamp}_{str(sha)[:12]}.jsonl.gz"
        dest.parent.mkdir(parents=True, exist_ok=True)
        with source_path.open("rb") as src, gzip.open(dest, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst)
        persisted = str(dest)

    sources["osm_brussels"] = {
        "status": summary.get("status"),
        "sha256": sha,
        "last_seen": iso_now(),
        "last_fetched": summary.get("last_fetched") or prior.get("last_fetched"),
        "last_changed": iso_now() if changed else prior.get("last_changed"),
        "materialized": summary.get("materialized", prior.get("materialized", 0)),
        "osm_unique_after_hub_dedupe": summary.get("osm_unique_after_hub_dedupe", 0),
        "cross_source_duplicates": summary.get("cross_source_duplicates", 0),
        "persisted_snapshot": persisted,
        "error": summary.get("error", ""),
    }
    dump_json(state_path, state)
    print(json.dumps({"status": "persisted", "changed": changed, "source": sources["osm_brussels"]}, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--hub-snapshot", required=True)
    prep.add_argument("--out-candidates", required=True)
    prep.add_argument("--plan-dir", default="results/fleet_plan")
    prep.add_argument("--max-age-hours", type=float, default=6.0)
    per = sub.add_parser("persist")
    per.add_argument("--plan-dir", default="results/fleet_plan")
    args = ap.parse_args()
    return prepare(args) if args.command == "prepare" else persist(args)


if __name__ == "__main__":
    raise SystemExit(main())
