#!/usr/bin/env python3
"""Strict premium/luxury gate for bulk hospitality candidates.

Input is the raw/enriched CSV produced by osm_pbf_hospitality_harvest.py.
The raw corpus is never deleted. This gate creates a separate shortlist for
high-end/luxury property-video prospecting.

Principles:
- Prefer luxury villas, premium vacation-rental/property managers, resorts,
  boutique/design hotels, serviced residences and visually strong properties.
- Do not promote a business merely because it has an email/website.
- Budget/economy lodging is explicitly rejected.
- Website text is used only as public evidence. No contact data is inferred.
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

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "EvergreenLeadMiner-PremiumGate/1.0 (+public-business-research)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.8",
}

BUDGET_PATTERNS = (
    "motel 6", "super 8", "travelodge", "days inn", "econo lodge",
    "rodeway inn", "quality inn", "comfort inn", "country inn",
    "holiday inn express", "hampton inn", "fairfield inn", "best western",
    "la quinta", "red roof", "courtyard by marriott", "home2 suites",
    "residence inn", "extended stay america", "howard johnson",
    "americas best value inn", "surestay", "springhill suites",
    "towneplace suites", "candlewood suites", "microtel", "econolodge",
    "budget inn", "budget lodge",
)

PREMIUM_BRANDS = (
    "four seasons", "ritz-carlton", "ritz carlton", "st. regis", "st regis",
    "waldorf astoria", "mandarin oriental", "rosewood", "auberge resorts",
    "auberge resort", "belmond", "aman", "amanresorts", "one&only",
    "one and only", "montage", "pendry", "viceroy", "edition hotel",
    "the edition", "peninsula hotel", "raffles", "fairmont", "park hyatt",
    "1 hotel", "proper hotel", "nobu hotel", "luxury collection",
)

STRONG_SIGNALS = {
    "luxury": 24,
    "luxurious": 20,
    "high-end": 22,
    "high end": 22,
    "upscale": 18,
    "five-star": 28,
    "five star": 28,
    "5-star": 28,
    "5 star": 28,
    "boutique hotel": 16,
    "boutique resort": 18,
    "luxury villa": 28,
    "luxury villas": 28,
    "private villa": 22,
    "private villas": 22,
    "exclusive villa": 24,
    "estate": 12,
    "private residence": 18,
    "private residences": 18,
    "luxury residence": 22,
    "luxury residences": 22,
    "penthouse": 18,
    "beachfront": 18,
    "oceanfront": 18,
    "ski-in ski-out": 18,
    "ski in ski out": 18,
    "private pool": 16,
    "infinity pool": 18,
    "designer hotel": 18,
    "design hotel": 14,
    "award-winning hotel": 14,
    "award winning hotel": 14,
}

OPERATOR_SIGNALS = (
    "vacation rental management", "vacation rentals", "short-term rental",
    "short term rental", "property management", "holiday home management",
    "holiday homes", "villa management", "villa rentals", "managed homes",
    "managed properties", "our properties", "our villas", "our homes",
    "portfolio of homes", "portfolio of properties", "collection of villas",
    "collection of homes", "luxury rentals", "serviced residences",
    "serviced apartments",
)

VISUAL_SIGNALS = (
    "ocean view", "ocean-view", "sea view", "sea-view", "mountain view",
    "mountain-view", "panoramic view", "panoramic views", "rooftop",
    "private pool", "infinity pool", "beachfront", "oceanfront",
    "waterfront", "ski-in ski-out", "ski in ski out", "architecture",
    "architectural", "designer", "spa", "terrace", "private beach",
)

BASE_FIELDS = [
    "source", "osm_id", "country", "region", "name", "operator", "brand",
    "hospitality_type", "city", "state", "postcode", "street", "housenumber",
    "website", "domain", "public_email_osm", "public_email_web",
    "email_source_url", "public_phone", "rooms", "beds", "stars",
    "score", "priority", "source_url", "notes",
]
EXTRA_FIELDS = [
    "premium_score", "premium_tier", "premium_selected",
    "premium_reasons", "premium_source_url",
]


def norm(value: object) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def number(value: object) -> float | None:
    text = norm(value)
    if not text:
        return None
    m = re.search(r"\d+(?:[.,]\d+)?", text)
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", "."))
    except Exception:
        return None


def fetch_public_text(url: str, timeout: int = 12) -> Tuple[str, str]:
    if not url:
        return "", ""
    attempts = [url]
    if url.startswith("https://"):
        attempts.append("http://" + url[len("https://"):])
    for candidate in attempts:
        try:
            r = requests.get(candidate, headers=HEADERS, timeout=timeout, allow_redirects=True)
            if r.status_code >= 400:
                continue
            ctype = (r.headers.get("content-type") or "").lower()
            if ctype and "html" not in ctype and "xhtml" not in ctype:
                continue
            soup = BeautifulSoup(r.text[:2_000_000], "html.parser")
            for tag in soup(["script", "style", "noscript", "svg"]):
                tag.decompose()
            title = norm(soup.title.get_text(" ", strip=True) if soup.title else "")
            meta = ""
            for attrs in (
                {"name": re.compile("^description$", re.I)},
                {"property": re.compile("^og:description$", re.I)},
            ):
                node = soup.find("meta", attrs=attrs)
                if node and node.get("content"):
                    meta += " " + norm(node.get("content"))
            text = norm(soup.get_text(" ", strip=True))
            return (r.url or candidate), norm(" ".join([title, meta, text[:180_000]]))
        except Exception:
            continue
    return "", ""


def has_phrase(text: str, phrase: str) -> bool:
    low = text.lower()
    p = phrase.lower().strip()
    if not p:
        return False
    pattern = r"(?<![a-z0-9])" + re.escape(p).replace(r"\ ", r"\s+") + r"(?![a-z0-9])"
    return re.search(pattern, low) is not None


def count_hits(text: str, phrases: Sequence[str]) -> List[str]:
    return [p for p in phrases if has_phrase(text, p)]


def evaluate(row: Dict[str, str], web_text: str = "", web_url: str = "") -> Dict[str, str]:
    name = norm(row.get("name"))
    typ = norm(row.get("hospitality_type")).lower()
    hay = " ".join([
        name, norm(row.get("operator")), norm(row.get("brand")),
        norm(row.get("website")), norm(row.get("domain")), web_text,
    ]).lower()

    reasons: List[str] = []
    score = 0

    hard_budget = count_hits(hay, BUDGET_PATTERNS)
    if typ in {"motel", "hostel"}:
        hard_budget.append("type:" + typ)

    brand_hits = count_hits(hay, PREMIUM_BRANDS)
    if brand_hits:
        score += 38
        reasons.append("premium_brand:" + ",".join(brand_hits[:3]))

    stars = number(row.get("stars"))
    if stars is not None:
        if stars >= 5:
            score += 42
            reasons.append("stars>=5")
        elif stars >= 4:
            score += 26
            reasons.append("stars>=4")

    type_points = {
        "resort": 16,
        "chalet": 18,
        "apartment": 8,
        "hotel": 4,
        "guest_house": 2,
    }.get(typ, 0)
    score += type_points
    if type_points:
        reasons.append("type:" + typ)

    strong_hits: List[str] = []
    strong_points = 0
    for phrase, points in STRONG_SIGNALS.items():
        if has_phrase(hay, phrase):
            strong_hits.append(phrase)
            strong_points += points
    if strong_hits:
        score += min(strong_points, 48)
        reasons.append("premium:" + ",".join(strong_hits[:6]))

    operator_hits = count_hits(hay, OPERATOR_SIGNALS)
    if operator_hits:
        score += min(18, 8 + 2 * len(operator_hits))
        reasons.append("portfolio:" + ",".join(operator_hits[:4]))

    visual_hits = count_hits(hay, VISUAL_SIGNALS)
    if visual_hits:
        score += min(16, 4 + 2 * len(visual_hits))
        reasons.append("visual:" + ",".join(visual_hits[:5]))

    soft = []
    for phrase, pts in (
        ("boutique", 10), ("collection", 7), ("retreat", 8),
        ("residences", 8), ("residence", 5), ("spa", 6),
        ("private", 4), ("exclusive", 8), ("curated", 7),
    ):
        if has_phrase(hay, phrase):
            score += pts
            soft.append(phrase)
    if soft:
        reasons.append("soft:" + ",".join(soft[:5]))

    has_premium_evidence = bool(brand_hits or strong_hits or (stars is not None and stars >= 4))
    has_operator_leverage = bool(operator_hits)
    has_visual_evidence = len(visual_hits) >= 2

    selected = False
    if not hard_budget:
        if brand_hits:
            selected = True
        elif stars is not None and stars >= 4 and score >= 36:
            selected = True
        elif has_premium_evidence and score >= 46:
            selected = True
        elif has_operator_leverage and has_premium_evidence and score >= 40:
            selected = True
        elif typ == "resort" and has_visual_evidence and score >= 44:
            selected = True

    if hard_budget:
        reasons.insert(0, "reject_budget:" + ",".join(hard_budget[:3]))

    if not selected:
        tier = "REJECT"
    elif score >= 82:
        tier = "S"
    elif score >= 58:
        tier = "A"
    else:
        tier = "B"

    out = dict(row)
    out["premium_score"] = str(min(score, 100))
    out["premium_tier"] = tier
    out["premium_selected"] = "YES" if selected else "NO"
    out["premium_reasons"] = " | ".join(reasons)
    out["premium_source_url"] = web_url
    return out


def write_csv(path: Path, rows: Iterable[Dict[str, str]], fields: Sequence[str]) -> int:
    count = 0
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
            count += 1
    return count


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--workers", type=int, default=36)
    ap.add_argument("--max-web-checks", type=int, default=0,
                    help="0 = all candidates with a website")
    args = ap.parse_args()

    input_path = Path(args.input)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    with input_path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    website_rows = [r for r in rows if norm(r.get("website"))]
    website_rows.sort(key=lambda r: int(float(norm(r.get("score")) or "0")), reverse=True)
    if args.max_web_checks > 0:
        website_rows = website_rows[:args.max_web_checks]

    web_evidence: Dict[int, Tuple[str, str]] = {}
    started = time.time()
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futures = {ex.submit(fetch_public_text, r.get("website") or ""): id(r) for r in website_rows}
        done = 0
        for fut in as_completed(futures):
            key = futures[fut]
            try:
                web_evidence[key] = fut.result()
            except Exception:
                web_evidence[key] = ("", "")
            done += 1
            if done % 100 == 0:
                print(f"Premium web checks {done}/{len(website_rows)}", flush=True)

    evaluated = []
    for row in rows:
        web_url, web_text = web_evidence.get(id(row), ("", ""))
        evaluated.append(evaluate(row, web_text=web_text, web_url=web_url))

    evaluated.sort(
        key=lambda r: (
            r["premium_selected"] != "YES",
            {"S": 0, "A": 1, "B": 2, "REJECT": 3}.get(r["premium_tier"], 4),
            -int(r["premium_score"]),
        )
    )

    fields = list(dict.fromkeys(BASE_FIELDS + EXTRA_FIELDS + list(rows[0].keys() if rows else [])))
    selected = [r for r in evaluated if r["premium_selected"] == "YES"]
    selected_email = [
        r for r in selected
        if norm(r.get("public_email_osm")) or norm(r.get("public_email_web"))
    ]
    rejected = [r for r in evaluated if r["premium_selected"] != "YES"]

    write_csv(outdir / "premium_all_scored.csv", evaluated, fields)
    write_csv(outdir / "premium_selected.csv", selected, fields)
    write_csv(outdir / "premium_selected_with_email.csv", selected_email, fields)
    write_csv(outdir / "premium_rejected.csv", rejected, fields)

    tiers = {tier: sum(1 for r in selected if r["premium_tier"] == tier) for tier in ("S", "A", "B")}
    summary = {
        "input_candidates": len(rows),
        "websites_checked_for_premium": len(website_rows),
        "premium_selected": len(selected),
        "premium_selected_with_public_email": len(selected_email),
        "premium_tiers": tiers,
        "rejected_nonpremium": len(rejected),
        "elapsed_seconds": round(time.time() - started, 2),
        "selection_rule": (
            "Strict luxury/high-end evidence gate. Email/website availability does not make a lead premium. "
            "Budget/economy chains and motel/hostel types are rejected."
        ),
    }
    (outdir / "premium_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
