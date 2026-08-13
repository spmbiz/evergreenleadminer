#!/usr/bin/env python3
"""High-end-first hospitality harvesting (V4).

Pipeline:
  PBF -> broad OSM hospitality extraction (NO broad website/email crawl) ->
  homepage-only premium screening -> deep email/contact enrichment ONLY for
  selected premium rows -> premium artifacts.

The point is economic precision: spend crawl/enrichment effort after a property
already looks high-end. Public emails are never guessed.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

import osm_pbf_hospitality_harvest as base

PREMIUM_BRANDS = (
    "four seasons", "ritz-carlton", "ritz carlton", "st. regis", "st regis",
    "waldorf astoria", "mandarin oriental", "rosewood", "auberge resorts",
    "auberge resort", "belmond", "aman", "one&only", "one and only",
    "montage", "pendry", "viceroy", "raffles", "park hyatt", "1 hotel",
    "proper hotel", "nobu hotel", "luxury collection", "edition hotel",
    "the edition", "fairmont", "six senses", "banyan tree", "capella",
    "cheval blanc", "dorchester collection", "oetker collection",
)

HARD_REJECT = (
    "motel 6", "super 8", "travelodge", "days inn", "econo lodge",
    "econolodge", "rodeway inn", "quality inn", "comfort inn",
    "holiday inn express", "hampton inn", "fairfield inn", "best western",
    "la quinta", "red roof", "extended stay america", "howard johnson",
    "americas best value inn", "surestay", "microtel", "budget inn",
    "budget lodge", "hostel", "backpacker", "student housing",
    "assisted living", "senior living", "campground", "camp site",
)

IDENTITY_PREMIUM = (
    "luxury", "ultra-luxury", "ultra luxury", "high-end", "high end",
    "five-star", "five star", "5-star", "5 star", "boutique hotel",
    "boutique resort", "design hotel", "designer hotel", "luxury resort",
    "luxury lodge", "luxury retreat", "luxury villa", "luxury villas",
    "private villa", "private villas", "exclusive villa", "estate",
    "luxury residence", "luxury residences", "private residence",
    "private residences", "penthouse", "serviced residence",
    "serviced residences", "private island",
)

PORTFOLIO_SIGNALS = (
    "vacation rentals", "vacation rental management", "short-term rental",
    "short term rental", "property management", "villa management",
    "villa rentals", "managed homes", "managed properties", "our properties",
    "our villas", "our homes", "portfolio of homes", "portfolio of properties",
    "collection of villas", "collection of homes", "luxury rentals",
    "serviced residences", "residences", "resorts & residences",
)

VISUAL_SIGNALS = (
    "beachfront", "oceanfront", "waterfront", "private pool", "infinity pool",
    "private beach", "ski-in ski-out", "ski in ski out", "panoramic views",
    "ocean view", "sea view", "mountain view", "penthouse", "rooftop terrace",
    "architect-designed", "architect designed", "private island",
)

PORTFOLIO_LINK_HINTS = (
    "properties", "villas", "homes", "residences", "resorts", "portfolio",
    "accommodation", "accommodations", "stays", "rentals",
)

V4_EXTRA = [
    "v4_score", "v4_tier", "v4_selected", "v4_master_ready",
    "v4_reasons", "v4_screen_source_url", "v4_portfolio_urls",
]


def norm(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def num(value: object) -> float | None:
    m = re.search(r"\d+(?:[.,]\d+)?", norm(value))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", "."))
    except Exception:
        return None


def has_phrase(text: str, phrase: str) -> bool:
    p = phrase.lower().strip()
    if not p:
        return False
    return re.search(r"(?<![a-z0-9])" + re.escape(p).replace(r"\ ", r"\s+") + r"(?![a-z0-9])", text.lower()) is not None


def hits(text: str, phrases: Sequence[str]) -> List[str]:
    return [p for p in phrases if has_phrase(text, p)]


def homepage_screen(url: str, timeout: int = 12) -> Tuple[str, str, str, str, List[str]]:
    """Fetch homepage only. Do not extract email yet.

    Returns final_url, identity_text(title/meta), body_text, html, portfolio_urls.
    """
    if not url:
        return "", "", "", "", []
    final_url, html = base.fetch_html(url, timeout=timeout)
    if not html and url.startswith("https://"):
        final_url, html = base.fetch_html("http://" + url[len("https://"):], timeout=timeout)
    if not html:
        return "", "", "", "", []

    soup = BeautifulSoup(html, "html.parser")
    title = norm(soup.title.get_text(" ", strip=True) if soup.title else "")
    metas: List[str] = []
    for attrs in (
        {"name": re.compile("^description$", re.I)},
        {"property": re.compile("^og:description$", re.I)},
        {"property": re.compile("^og:title$", re.I)},
    ):
        node = soup.find("meta", attrs=attrs)
        if node and node.get("content"):
            metas.append(norm(node.get("content")))

    links: List[str] = []
    seen = set()
    root = base.root_domain(final_url or url)
    for a in soup.find_all("a", href=True):
        href = str(a.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        full = urljoin(final_url or url, href)
        if base.root_domain(full) != root:
            continue
        hay = (full + " " + norm(a.get_text(" ", strip=True))).lower()
        if not any(h in hay for h in PORTFOLIO_LINK_HINTS):
            continue
        clean = urlparse(full)._replace(fragment="").geturl()
        if clean not in seen:
            seen.add(clean)
            links.append(clean)
        if len(links) >= 5:
            break

    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    body = norm(soup.get_text(" ", strip=True))[:180000]
    identity = norm(" ".join([title] + metas))
    return final_url or url, identity, body, html, links


def prefilter_hint(row: Dict[str, str]) -> bool:
    typ = norm(row.get("hospitality_type")).lower()
    if typ in {"hostel", "motel"}:
        return False
    identity = norm(" ".join([
        row.get("name", ""), row.get("operator", ""), row.get("brand", ""),
        row.get("domain", ""), row.get("website", ""),
    ])).lower()
    if hits(identity, HARD_REJECT):
        return False
    stars = num(row.get("stars"))
    if stars is not None and stars >= 4:
        return True
    if hits(identity, PREMIUM_BRANDS) or hits(identity, IDENTITY_PREMIUM):
        return True
    # Website-bearing resorts/hotels get a cheap homepage screen; apartments and
    # guest houses need some initial luxury/operator clue to avoid huge noise.
    if row.get("website") and typ in {"resort", "hotel", "chalet"}:
        return True
    if row.get("website") and typ in {"apartment", "guest_house", "hospitality"}:
        return any(k in identity for k in ("villa", "residence", "resort", "retreat", "lodge", "vacation", "holiday", "luxury", "boutique", "estate"))
    return False


def evaluate(row: Dict[str, str], identity: str, body: str, screen_url: str, portfolio_urls: Sequence[str]) -> Dict[str, str]:
    typ = norm(row.get("hospitality_type")).lower()
    name = norm(row.get("name"))
    strong = norm(" ".join([name, row.get("operator", ""), row.get("brand", ""), identity])).lower()
    full = strong + " " + body.lower()
    reasons: List[str] = []
    score = 0

    bad = hits(strong, HARD_REJECT)
    if typ in {"hostel", "motel"}:
        bad.append("type:" + typ)

    pbrands = hits(strong, PREMIUM_BRANDS)
    idprem = hits(strong, IDENTITY_PREMIUM)
    bodyprem = hits(body[:60000], IDENTITY_PREMIUM)
    portfolio = hits(full, PORTFOLIO_SIGNALS)
    visual = hits(full, VISUAL_SIGNALS)
    stars = num(row.get("stars"))

    if pbrands:
        score += 60
        reasons.append("brand:" + ",".join(pbrands[:2]))
    if stars is not None and stars >= 5:
        score += 52
        reasons.append("5-star")
    elif stars is not None and stars >= 4:
        score += 38
        reasons.append("4-star")
    if idprem:
        score += min(44, 30 + 5 * (len(idprem) - 1))
        reasons.append("identity:" + ",".join(idprem[:4]))
    elif bodyprem:
        score += min(24, 15 + 3 * (len(bodyprem) - 1))
        reasons.append("page-premium:" + ",".join(bodyprem[:4]))
    if portfolio:
        score += min(30, 12 + 3 * len(portfolio))
        reasons.append("portfolio:" + ",".join(portfolio[:4]))
    if portfolio_urls:
        score += min(14, 4 + 2 * len(portfolio_urls))
        reasons.append("portfolio-links:" + str(len(portfolio_urls)))
    if visual:
        score += min(20, 5 + 3 * len(visual))
        reasons.append("visual:" + ",".join(visual[:4]))
    if typ == "resort":
        score += 10
    elif typ == "hotel":
        score += 4
    elif typ == "chalet":
        score += 6

    rooms = num(row.get("rooms"))
    if rooms and rooms >= 20 and (pbrands or idprem or bodyprem or (stars and stars >= 4)):
        score += 6

    premium_evidence = bool(pbrands or idprem or bodyprem or (stars is not None and stars >= 4))
    operator_leverage = bool(portfolio or portfolio_urls)
    visual_evidence = len(visual) >= 2

    selected = False
    if not bad:
        if pbrands or (stars is not None and stars >= 5):
            selected = score >= 50
        elif typ == "resort":
            selected = premium_evidence and score >= 48
        elif typ == "hotel":
            selected = premium_evidence and score >= 52
        elif typ in {"apartment", "guest_house", "chalet", "hospitality"}:
            selected = premium_evidence and score >= 62 and (operator_leverage or visual_evidence)
        else:
            selected = premium_evidence and score >= 60

    if bad:
        reasons.insert(0, "reject:" + ",".join(bad[:3]))
    if not selected:
        tier = "REJECT"
    elif score >= 92:
        tier = "S"
    elif score >= 72:
        tier = "A"
    else:
        tier = "B"

    out = dict(row)
    out.update({
        "v4_score": str(min(score, 100)),
        "v4_tier": tier,
        "v4_selected": "YES" if selected else "NO",
        "v4_master_ready": "NO",
        "v4_reasons": " | ".join(reasons),
        "v4_screen_source_url": screen_url,
        "v4_portfolio_urls": " | ".join(portfolio_urls),
    })
    return out


def enrich_selected(row: Dict[str, str], cached_html: str, final_url: str) -> Tuple[str, str, str]:
    if row.get("public_email_osm"):
        return row.get("public_email_osm") or "", row.get("source_url") or "", "osm_email"
    website = row.get("website") or ""
    if not website:
        return "", "", "no_website"

    html = cached_html
    source = final_url or website
    if not html:
        source, html = base.fetch_html(website)
    if not html:
        return "", "", "homepage_fetch_failed"

    soup = BeautifulSoup(html, "html.parser")
    emails = base.extract_emails(html, soup)
    if emails:
        return emails[0], source, "homepage"

    for link in base.same_domain_links(source, soup, max_links=4):
        u, txt = base.fetch_html(link)
        if not txt:
            continue
        sp = BeautifulSoup(txt, "html.parser")
        emails = base.extract_emails(txt, sp)
        if emails:
            return emails[0], u or link, "contact_page"
    return "", "", "no_public_email_found"


def write_csv(path: Path, rows: Iterable[Dict[str, str]], fields: Sequence[str]) -> int:
    n = 0
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(fields), extrasaction="ignore")
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
    ap.add_argument("--max-candidates", type=int, default=30000)
    ap.add_argument("--max-screen", type=int, default=12000)
    ap.add_argument("--workers", type=int, default=36)
    args = ap.parse_args()

    pbf = Path(args.pbf)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    rows = base.extract_candidates(pbf, outdir, args.country, args.region, args.max_candidates)
    broad_count = write_csv(outdir / "v4_raw_broad.csv", rows, base.CSV_FIELDS)

    screen_pool = [r for r in rows if prefilter_hint(r)]
    screen_pool.sort(key=lambda r: int(float(norm(r.get("score")) or 0)), reverse=True)
    if args.max_screen > 0:
        screen_pool = screen_pool[:args.max_screen]

    evidence: Dict[int, Tuple[str, str, str, str, List[str]]] = {}
    website_targets = [r for r in screen_pool if r.get("website")]
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        fs = {ex.submit(homepage_screen, r.get("website") or ""): id(r) for r in website_targets}
        for i, fut in enumerate(as_completed(fs), 1):
            try:
                evidence[fs[fut]] = fut.result()
            except Exception:
                evidence[fs[fut]] = ("", "", "", "", [])
            if i % 100 == 0:
                print(f"V4 premium screens {i}/{len(website_targets)}", flush=True)

    scored: List[Dict[str, str]] = []
    cache: Dict[int, Tuple[str, str]] = {}
    for row in screen_pool:
        final_url, ident, body, html, portfolio_urls = evidence.get(id(row), ("", "", "", "", []))
        out = evaluate(row, ident, body, final_url, portfolio_urls)
        scored.append(out)
        cache[id(out)] = (html, final_url)

    scored.sort(key=lambda r: (r["v4_selected"] != "YES", -int(r["v4_score"])))
    selected = [r for r in scored if r["v4_selected"] == "YES"]

    # Deep email/contact crawl happens ONLY now, after premium selection.
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        fs = {}
        for row in selected:
            html, final_url = cache.get(id(row), ("", ""))
            fs[ex.submit(enrich_selected, row, html, final_url)] = row
        for i, fut in enumerate(as_completed(fs), 1):
            row = fs[fut]
            try:
                email, source_url, note = fut.result()
            except Exception as exc:
                email, source_url, note = "", "", f"error:{type(exc).__name__}"
            if email and not row.get("public_email_osm"):
                row["public_email_web"] = email
                row["email_source_url"] = source_url
            row["notes"] = (norm(row.get("notes")) + "; v4:" + note).strip("; ")
            has_email = bool(norm(row.get("public_email_osm")) or norm(row.get("public_email_web")))
            has_site = bool(norm(row.get("website")))
            row["v4_master_ready"] = "YES" if has_email and has_site and row["v4_tier"] in {"S", "A"} else "NO"
            if i % 100 == 0:
                print(f"V4 selected enrichment {i}/{len(selected)}", flush=True)

    fields = list(dict.fromkeys(base.CSV_FIELDS + V4_EXTRA))
    selected.sort(key=lambda r: (r["v4_master_ready"] != "YES", -int(r["v4_score"])))
    with_email = [r for r in selected if norm(r.get("public_email_osm")) or norm(r.get("public_email_web"))]
    ready = [r for r in selected if r.get("v4_master_ready") == "YES"]
    rejected = [r for r in scored if r["v4_selected"] != "YES"]

    write_csv(outdir / "v4_scored_screen_pool.csv", scored, fields)
    write_csv(outdir / "v4_selected.csv", selected, fields)
    write_csv(outdir / "v4_selected_with_email.csv", with_email, fields)
    write_csv(outdir / "v4_master_ready.csv", ready, fields)
    write_csv(outdir / "v4_rejected_after_screen.csv", rejected, fields)

    portfolio_candidates = sum(1 for r in selected if norm(r.get("v4_portfolio_urls")))
    summary = {
        "country": args.country,
        "region": args.region,
        "raw_broad": broad_count,
        "cheap_prefilter_pool": len(screen_pool),
        "homepage_screens": len(website_targets),
        "high_end_selected_before_email": len(selected),
        "selected_with_public_email": len(with_email),
        "master_ready_sa": len(ready),
        "portfolio_expansion_candidates": portfolio_candidates,
        "tiers": {t: sum(1 for r in selected if r["v4_tier"] == t) for t in ("S", "A", "B")},
        "email_yield_on_selected": round((len(with_email) / len(selected)), 4) if selected else 0,
        "selected_rate_from_raw": round((len(selected) / broad_count), 4) if broad_count else 0,
        "elapsed_seconds": round(time.time() - t0, 2),
        "rule": "High-end-first: no broad website email crawl. Premium screening precedes deep email/contact enrichment; budget/hostel/motel rejected; public emails only.",
    }
    (outdir / "v4_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
