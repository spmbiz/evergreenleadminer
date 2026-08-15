#!/usr/bin/env python3
"""Bounded second-pass enrichment for already-canonical hospitality leads.

Purpose:
- keep discovery workers focused on net-new accounts;
- revisit canonical first-party websites to fill missing Instagram, Facebook,
  contact page, published WhatsApp and portfolio/listing URLs;
- persist those fields durably in canonical SQLite so the Sheet bridge can
  materialize updates.

Integrity invariants:
- public first-party HTTP(S) only for crawled pages;
- no login, forms, CAPTCHA bypass, JS automation or guessed contacts;
- WhatsApp must be an explicit published link;
- portfolio/listing URL must be an explicit link discovered on the site;
- existing non-empty canonical fields are never blanked by a failed revisit.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin, urlparse

from v6_public_contact_enrich import (
    CONTACT_HINTS,
    extract_links,
    host,
    root_host,
    safe_get,
    social_profiles,
)

PORTFOLIO_HINTS = (
    "vacation-rental", "vacation-rentals", "holiday-rental", "holiday-rentals",
    "short-term", "shortterm", "our-properties", "properties", "property-listings",
    "portfolio", "our-homes", "homes", "stays", "accommodation", "accommodations",
    "villas", "villa", "cabins", "chalets", "cottages", "apartments", "suites",
    "rooms", "rentals", "destinations", "book", "booking",
)
BAD_PORTFOLIO_HINTS = (
    "contact", "about", "privacy", "terms", "blog", "news", "careers", "login",
    "owner-login", "guest-login", "faq", "cookie", "sitemap",
)
WHATSAPP_HOSTS = {"wa.me", "api.whatsapp.com", "web.whatsapp.com", "whatsapp.com"}
TARGET_FIELDS = ("instagram", "facebook", "contact_page", "whatsapp", "portfolio_url")


def now_z() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def norm(v) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()


def ensure_schema(con: sqlite3.Connection) -> None:
    cols = {r[1] for r in con.execute("PRAGMA table_info(leads)")}
    wanted = {
        "facebook": "TEXT",
        "contact_page": "TEXT",
        "whatsapp": "TEXT",
        "portfolio_url": "TEXT",
        "multichannel_last_attempt": "TEXT",
        "multichannel_last_success": "TEXT",
        "multichannel_status": "TEXT",
    }
    for name, typ in wanted.items():
        if name not in cols:
            con.execute(f"ALTER TABLE leads ADD COLUMN {name} {typ}")
    con.commit()

    # One-time/ongoing conservative backfill from already-persisted raw observations.
    rows = con.execute(
        "SELECT domain,raw_json,facebook,contact_page,whatsapp,portfolio_url FROM leads"
    ).fetchall()
    for row in rows:
        try:
            raw = json.loads(row[1] or "{}")
        except Exception:
            raw = {}
        if not isinstance(raw, dict):
            continue
        vals = {
            "facebook": row[2] or raw.get("facebook") or "",
            "contact_page": row[3] or raw.get("contact_page") or "",
            "whatsapp": row[4] or raw.get("whatsapp") or raw.get("whatsapp_url") or "",
            "portfolio_url": row[5] or raw.get("portfolio_url") or raw.get("portfolio_listing_url") or raw.get("listing_url") or "",
        }
        if any(vals.values()):
            con.execute(
                "UPDATE leads SET facebook=COALESCE(NULLIF(facebook,''),?), "
                "contact_page=COALESCE(NULLIF(contact_page,''),?), "
                "whatsapp=COALESCE(NULLIF(whatsapp,''),?), "
                "portfolio_url=COALESCE(NULLIF(portfolio_url,''),?) WHERE domain=?",
                (vals["facebook"], vals["contact_page"], vals["whatsapp"], vals["portfolio_url"], row[0]),
            )
    con.commit()


def clean_url(url: str) -> str:
    try:
        p = urlparse(url)
        return p._replace(fragment="").geturl()
    except Exception:
        return url


def explicit_whatsapp(raw_html: str, base_url: str) -> str:
    for _href, url in extract_links(raw_html or "", base_url):
        try:
            p = urlparse(url)
            h = (p.hostname or "").lower().strip(".")
            if h.startswith("www."):
                h = h[4:]
            if h in WHATSAPP_HOSTS or h.endswith(".whatsapp.com"):
                # The link itself is the evidence; do not infer a phone number.
                if p.path.strip("/") or p.query:
                    return clean_url(url)
        except Exception:
            continue
    return ""


def ranked_site_links(raw_html: str, base_url: str, allowed_root: str):
    contact = []
    portfolio = []
    seen = set()
    for href, url in extract_links(raw_html or "", base_url):
        try:
            p = urlparse(url)
            h = (p.hostname or "").lower().strip(".")
            if root_host(h) != allowed_root:
                continue
            clean = clean_url(url)
            if clean in seen:
                continue
            seen.add(clean)
            hay = (href + " " + (p.path or "") + " " + (p.query or "")).lower()
            cscore = sum(1 for x in CONTACT_HINTS if x in hay)
            pscore = sum(1 for x in PORTFOLIO_HINTS if x in hay) - sum(2 for x in BAD_PORTFOLIO_HINTS if x in hay)
            if cscore:
                contact.append((cscore, clean))
            if pscore > 0:
                # Avoid treating the homepage as a portfolio/listing URL.
                if (p.path or "/").strip("/"):
                    portfolio.append((pscore, clean))
        except Exception:
            continue
    contact.sort(key=lambda x: (-x[0], len(x[1]), x[1]))
    portfolio.sort(key=lambda x: (-x[0], len(x[1]), x[1]))
    return [u for _, u in contact], [u for _, u in portfolio]


def merge_nonempty(base: dict, found: dict) -> dict:
    out = dict(base)
    for key in TARGET_FIELDS:
        if not norm(out.get(key)) and norm(found.get(key)):
            out[key] = norm(found[key])
    return out


def enrich_one(row: dict, timeout: float, max_pages: int, max_bytes: int) -> dict:
    website = norm(row.get("website"))
    allowed_root = root_host(host(website))
    result = {k: norm(row.get(k)) for k in TARGET_FIELDS}
    result.update({"domain": row.get("domain"), "status": "NO_CHANGE", "pages_fetched": 0, "reason": ""})
    if not website or not allowed_root:
        result.update(status="FAILED", reason="INVALID_WEBSITE")
        return result

    home, reason = safe_get(website, allowed_root, timeout, max_bytes)
    if not home:
        result.update(status="FAILED", reason=reason)
        return result

    pages = [home]
    contact_links, portfolio_links = ranked_site_links(home["text"], home["url"], allowed_root)

    # Record explicit destination links even when we do not need to fetch all of them.
    if not result.get("contact_page") and contact_links:
        result["contact_page"] = contact_links[0]
    if not result.get("portfolio_url") and portfolio_links:
        result["portfolio_url"] = portfolio_links[0]

    candidates = []
    for u in contact_links[:3] + portfolio_links[:3]:
        if u not in candidates:
            candidates.append(u)
    for fallback in ("/contact", "/contact-us", "/about", "/properties", "/vacation-rentals"):
        u = urljoin(home["url"], fallback)
        if u not in candidates:
            candidates.append(u)

    for url in candidates:
        if len(pages) >= max_pages:
            break
        if any(p["url"] == url for p in pages):
            continue
        page, _ = safe_get(url, allowed_root, timeout, max_bytes)
        if page:
            pages.append(page)

    found = {}
    for page in pages:
        ig, fb = social_profiles(page["text"], page["url"])
        if ig and not found.get("instagram"):
            found["instagram"] = ig
        if fb and not found.get("facebook"):
            found["facebook"] = fb
        wa = explicit_whatsapp(page["text"], page["url"])
        if wa and not found.get("whatsapp"):
            found["whatsapp"] = wa
        c, p = ranked_site_links(page["text"], page["url"], allowed_root)
        if c and not found.get("contact_page"):
            found["contact_page"] = c[0]
        if p and not found.get("portfolio_url"):
            found["portfolio_url"] = p[0]

    before = {k: norm(row.get(k)) for k in TARGET_FIELDS}
    merged = merge_nonempty(before, {**result, **found})
    gained = [k for k in TARGET_FIELDS if not before.get(k) and merged.get(k)]
    result.update(merged)
    result["pages_fetched"] = len(pages)
    result["gained"] = gained
    result["status"] = "ENRICHED" if gained else "NO_CHANGE"
    result["reason"] = "PUBLIC_FIRST_PARTY_LINKS" if gained else "NO_NEW_PUBLIC_MULTICHANNEL_FIELDS"
    return result


def pick_rows(con: sqlite3.Connection, batch_size: int, retry_hours: int):
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=retry_hours)).isoformat().replace("+00:00", "Z")
    con.row_factory = sqlite3.Row
    q = """
    SELECT * FROM leads
    WHERE website IS NOT NULL AND website<>''
      AND (COALESCE(instagram,'')='' OR COALESCE(facebook,'')='' OR COALESCE(contact_page,'')=''
           OR COALESCE(whatsapp,'')='' OR COALESCE(portfolio_url,'')='')
      AND (multichannel_last_attempt IS NULL OR multichannel_last_attempt='' OR multichannel_last_attempt<?)
    ORDER BY CASE WHEN fit_tier='A' THEN 0 WHEN fit_tier='B' THEN 1 ELSE 2 END,
             operator_score DESC, premium_score DESC, first_seen ASC, domain ASC
    LIMIT ?
    """
    return [dict(r) for r in con.execute(q, (cutoff, max(1, batch_size))).fetchall()]


def update_metrics(path: Path, summary: dict) -> None:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        doc = {}
    doc["multichannel_enrichment"] = summary
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--canonical-db", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--batch-size", type=int, default=700)
    ap.add_argument("--workers", type=int, default=64)
    ap.add_argument("--timeout", type=float, default=6.0)
    ap.add_argument("--max-pages", type=int, default=3)
    ap.add_argument("--max-bytes", type=int, default=750000)
    ap.add_argument("--retry-hours", type=int, default=72)
    ap.add_argument("--metrics", default="metrics/latest.json")
    a = ap.parse_args()

    t0 = time.time()
    db = Path(a.canonical_db)
    outdir = Path(a.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    ensure_schema(con)
    rows = pick_rows(con, a.batch_size, a.retry_hours)
    attempted_at = now_z()

    results = []
    with ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
        futures = [ex.submit(enrich_one, r, a.timeout, a.max_pages, a.max_bytes) for r in rows]
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as e:
                results.append({"domain": "", "status": "FAILED", "reason": type(e).__name__, "pages_fetched": 0, **{k: "" for k in TARGET_FIELDS}})

    gained_counts = {k: 0 for k in TARGET_FIELDS}
    enriched_domains = 0
    for res in results:
        domain = norm(res.get("domain"))
        if not domain:
            continue
        prior = con.execute("SELECT raw_json FROM leads WHERE domain=?", (domain,)).fetchone()
        try:
            raw = json.loads((prior[0] if prior else "") or "{}")
        except Exception:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        gained = list(res.get("gained") or [])
        for k in gained:
            gained_counts[k] += 1
        if gained:
            enriched_domains += 1
        for k in TARGET_FIELDS:
            if norm(res.get(k)):
                raw[k] = norm(res[k])
        raw["multichannel_enrichment_status"] = res.get("status")
        raw["multichannel_enrichment_reason"] = res.get("reason")
        raw["multichannel_enrichment_last_attempt"] = attempted_at
        success = attempted_at if gained else None
        con.execute(
            "UPDATE leads SET "
            "instagram=COALESCE(NULLIF(instagram,''),?), facebook=COALESCE(NULLIF(facebook,''),?), "
            "contact_page=COALESCE(NULLIF(contact_page,''),?), whatsapp=COALESCE(NULLIF(whatsapp,''),?), "
            "portfolio_url=COALESCE(NULLIF(portfolio_url,''),?), multichannel_last_attempt=?, "
            "multichannel_last_success=CASE WHEN ? IS NOT NULL THEN ? ELSE multichannel_last_success END, "
            "multichannel_status=?, raw_json=? WHERE domain=?",
            (
                norm(res.get("instagram")), norm(res.get("facebook")), norm(res.get("contact_page")),
                norm(res.get("whatsapp")), norm(res.get("portfolio_url")), attempted_at,
                success, success, res.get("status"), json.dumps(raw, ensure_ascii=False), domain,
            ),
        )
    con.commit()

    remaining = con.execute(
        "SELECT COUNT(*) FROM leads WHERE website IS NOT NULL AND website<>'' AND "
        "(COALESCE(instagram,'')='' OR COALESCE(facebook,'')='' OR COALESCE(contact_page,'')='' "
        "OR COALESCE(whatsapp,'')='' OR COALESCE(portfolio_url,'')='')"
    ).fetchone()[0]
    con.close()

    summary = {
        "attempted": len(rows),
        "domains_enriched": enriched_domains,
        "instagram_added": gained_counts["instagram"],
        "facebook_added": gained_counts["facebook"],
        "contact_page_added": gained_counts["contact_page"],
        "whatsapp_added": gained_counts["whatsapp"],
        "portfolio_url_added": gained_counts["portfolio_url"],
        "pages_fetched": sum(int(r.get("pages_fetched") or 0) for r in results),
        "failed": sum(r.get("status") == "FAILED" for r in results),
        "incomplete_domains_remaining": int(remaining),
        "retry_hours": a.retry_hours,
        "elapsed_seconds": round(time.time() - t0, 2),
    }
    (outdir / "multichannel_enrichment_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    update_metrics(Path(a.metrics), summary)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
