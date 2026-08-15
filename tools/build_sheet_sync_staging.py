#!/usr/bin/env python3
"""Build deterministic, chunked TSV staging for MASTER Google Sheet sync.

Reads the durable GitHub sheet-sync queue, collapses historical/repeated rows
using the same identity precedence as MASTER, and emits Sheets-friendly TSV
chunks. It does NOT decide whether a row already exists in MASTER; that final
check happens live in the Sheet against Enriched Leads + Dedupe Index.
"""
from __future__ import annotations

import argparse
import json
import re
import unicodedata
from pathlib import Path
from urllib.parse import urlparse

GENERIC_DOMAINS = {
    "google.com", "facebook.com", "instagram.com", "airbnb.com", "booking.com",
    "tripadvisor.com", "vrbo.com", "expedia.com", "yelp.com", "linktr.ee",
    "wixsite.com", "wordpress.com",
}


def norm_text(value) -> str:
    s = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def norm_phone(value) -> str:
    return re.sub(r"\D+", "", str(value or ""))


def norm_domain(value) -> str:
    s = str(value or "").strip().lower()
    if not s:
        return ""
    if "://" not in s:
        s = "http://" + s
    try:
        host = (urlparse(s).hostname or "").lower().strip(".")
    except Exception:
        host = ""
    if host.startswith("www."):
        host = host[4:]
    return "" if host in GENERIC_DOMAINS else host


def record_domain(r: dict) -> str:
    return norm_domain(r.get("domain")) or norm_domain(r.get("website")) or norm_domain(r.get("final_url"))


def source_ref(r: dict) -> str:
    overture = str(r.get("overture_id") or "").strip()
    if overture:
        return f"overture:{overture}"
    source_record = str(r.get("source_record_id") or "").strip()
    if source_record:
        return source_record
    atp = str(r.get("atp_id") or "").strip()
    return f"atp:{atp}" if atp else ""


def richness(r: dict):
    fields = (
        "website", "public_email", "public_phone", "instagram", "facebook",
        "whatsapp", "contact_page", "portfolio_url", "operator", "final_url", "source_url",
    )
    nonempty = sum(bool(str(r.get(k) or "").strip()) for k in fields)
    fit = {"A": 2, "B": 1}.get(str(r.get("fit_tier") or "").upper(), 0)
    ts = str(r.get("last_seen") or r.get("_sheet_sync_queued_at") or "")
    return (nonempty, fit, ts, int(r.get("__seq") or 0))


def clean(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value).replace("\t", " ").replace("\r", " ").replace("\n", " ")


def canonical_row(r: dict) -> list[str]:
    fit = str(r.get("fit_tier") or "").upper()
    score = "91" if fit == "A" else "78" if fit == "B" else ""
    market = str(r.get("city") or r.get("region") or "").strip()
    country = str(r.get("country") or "").strip()
    live = str(r.get("live_status") or "").strip().title()
    enrich_status = "Verified" if live == "High" else "Recall-first" if live == "Medium" else "Qualification"
    sources = []
    for key in ("source_url", "instagram_source_url", "facebook_source_url", "email_source_url", "directory_url"):
        v = str(r.get(key) or "").strip()
        if v and v not in sources:
            sources.append(v)
    fit_note = f"Canonical queue drain | fit {fit} | GitHub MASTER dedupe passed"
    # 38 canonical columns A:AL. Blank means the source did not support a value.
    return [
        "", fit, score, market, country, str(r.get("name") or "").strip(), "",
        str(r.get("category") or "").strip(), str(r.get("street") or "").strip(),
        str(r.get("public_phone") or "").strip(), "", "", "", "", "", "", "",
        source_ref(r), str(r.get("last_seen") or r.get("first_seen") or "").strip(), "", fit_note,
        str(r.get("website") or r.get("final_url") or "").strip(),
        str(r.get("public_email") or "").strip(), str(r.get("contact_page") or "").strip(),
        str(r.get("instagram") or "").strip(), str(r.get("facebook") or "").strip(),
        str(r.get("whatsapp") or "").strip(), str(r.get("portfolio_url") or "").strip(),
        str(r.get("operator") or "").strip(), "", "", live, enrich_status,
        "; ".join(sources), str(r.get("notes") or r.get("live_reason") or "").strip(), "", "", "",
    ]


def primary_key(r: dict) -> str:
    domain = record_domain(r)
    phone = norm_phone(r.get("public_phone"))
    name = norm_text(r.get("name"))
    market = str(r.get("city") or r.get("region") or "").strip()
    country = str(r.get("country") or "").strip()
    if domain:
        return "domain:" + domain
    if phone and len(phone) >= 7:
        return "phone:" + phone
    if name and market and country:
        return "name:" + name + "|" + norm_text(market) + "|" + norm_text(country)
    return ""


def load_records(queue: Path) -> list[dict]:
    rows = []
    seq = 0
    for path in sorted(queue.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            rec["__seq"] = seq
            seq += 1
            rows.append(rec)
    return rows


def collapse(rows: list[dict]):
    seen_src, seen_domain, seen_phone, seen_name = set(), set(), set(), set()
    kept = []
    rejected = {"source_ref": 0, "domain": 0, "phone": 0, "name_market_country": 0}
    for r in sorted(rows, key=richness, reverse=True):
        src = source_ref(r).lower()
        domain = record_domain(r)
        phone = norm_phone(r.get("public_phone"))
        if len(phone) < 7:
            phone = ""
        name = norm_text(r.get("name"))
        market = str(r.get("city") or r.get("region") or "").strip()
        country = str(r.get("country") or "").strip()
        name_key = (name, norm_text(market), norm_text(country)) if name and market and country else None
        reason = None
        if src and src in seen_src:
            reason = "source_ref"
        elif domain and domain in seen_domain:
            reason = "domain"
        elif phone and phone in seen_phone:
            reason = "phone"
        elif name_key and name_key in seen_name:
            reason = "name_market_country"
        if reason:
            rejected[reason] += 1
            continue
        kept.append(r)
        if src:
            seen_src.add(src)
        if domain:
            seen_domain.add(domain)
        if phone:
            seen_phone.add(phone)
        if name_key:
            seen_name.add(name_key)
    return kept, rejected


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue", default="gpt/sheet_sync_queue")
    ap.add_argument("--out", default="staging/sheet_sync_latest")
    ap.add_argument("--chunk-rows", type=int, default=500)
    args = ap.parse_args()
    queue = Path(args.queue)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for p in out.glob("part-*.tsv"):
        p.unlink()

    raw = load_records(queue)
    kept, collapsed = collapse(raw)
    status_counts = {}
    fit_counts = {}
    for r in kept:
        status = str(r.get("live_status") or "").upper()
        status_counts[status] = status_counts.get(status, 0) + 1
        fit = str(r.get("fit_tier") or "").upper()
        fit_counts[fit] = fit_counts.get(fit, 0) + 1

    parts = []
    chunk_rows = max(100, int(args.chunk_rows))
    for start in range(0, len(kept), chunk_rows):
        batch = kept[start:start + chunk_rows]
        part = out / f"part-{start // chunk_rows:03d}.tsv"
        with part.open("w", encoding="utf-8", newline="") as fh:
            for r in batch:
                market = str(r.get("city") or r.get("region") or "").strip()
                country = str(r.get("country") or "").strip()
                phone = norm_phone(r.get("public_phone"))
                if len(phone) < 7:
                    phone = ""
                values = canonical_row(r) + [
                    source_ref(r).lower(), record_domain(r), phone, norm_text(r.get("name")),
                    market, country, str(r.get("live_status") or "").upper(), primary_key(r),
                ]
                fh.write("\t".join(clean(v) for v in values) + "\n")
        parts.append({"file": part.name, "rows": len(batch), "start_index": start})

    manifest = {
        "schema_version": 1,
        "raw_rows": len(raw),
        "unique_rows": len(kept),
        "within_queue_collapsed": len(raw) - len(kept),
        "collapse_reasons": collapsed,
        "fit_counts": fit_counts,
        "live_status_counts": status_counts,
        "column_count": 46,
        "parts": parts,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
