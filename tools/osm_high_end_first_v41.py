#!/usr/bin/env python3
"""V4.1 precision guard for the high-end-first hospitality harvester.

Keeps the V4 pipeline but hardens luxury-brand evidence: a premium brand may
only boost scoring when it is supported by OSM operator/brand metadata or by
the property's website/domain URL. A brand-looking property name or incidental
homepage/meta text is not sufficient. This prevents false positives such as an
independent lodge whose name happens to contain "Four Seasons".
"""
from __future__ import annotations

import re
from typing import Dict, List, Sequence

import osm_high_end_first_v4 as v4


def verified_brand_hits(row: Dict[str, str]) -> List[str]:
    metadata = v4.norm(" ".join([row.get("operator", ""), row.get("brand", "")])).lower()
    urlish = v4.norm(" ".join([row.get("domain", ""), row.get("website", "")])).lower()
    compact_urlish = re.sub(r"[^a-z0-9]", "", urlish)
    out: List[str] = []
    for phrase in v4.PREMIUM_BRANDS:
        if v4.has_phrase(metadata, phrase):
            out.append(phrase)
            continue
        token = re.sub(r"[^a-z0-9]", "", phrase.lower())
        if len(token) >= 4 and token in compact_urlish:
            out.append(phrase)
    return list(dict.fromkeys(out))


def evaluate(row: Dict[str, str], identity: str, body: str, screen_url: str, portfolio_urls: Sequence[str]) -> Dict[str, str]:
    typ = v4.norm(row.get("hospitality_type")).lower()
    name = v4.norm(row.get("name"))
    canonical = v4.norm(" ".join([
        name, row.get("operator", ""), row.get("brand", ""),
        row.get("domain", ""), row.get("website", ""),
    ])).lower()
    page_identity = v4.norm(identity).lower()
    strong = canonical + " " + page_identity
    full = strong + " " + body.lower()
    reasons: List[str] = []
    score = 0

    bad = v4.hits(canonical, v4.HARD_REJECT)
    if typ in {"hostel", "motel"}:
        bad.append("type:" + typ)

    pbrands = verified_brand_hits(row)
    idprem = v4.hits(strong, v4.IDENTITY_PREMIUM)
    bodyprem = v4.hits(body[:60000], v4.IDENTITY_PREMIUM)
    portfolio = v4.hits(full, v4.PORTFOLIO_SIGNALS)
    visual = v4.hits(full, v4.VISUAL_SIGNALS)
    stars = v4.num(row.get("stars"))

    if pbrands:
        score += 60
        reasons.append("brand-verified:" + ",".join(pbrands[:2]))
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

    rooms = v4.num(row.get("rooms"))
    if rooms and rooms >= 20 and (pbrands or idprem or bodyprem or (stars and stars >= 4)):
        score += 6

    premium_evidence = bool(pbrands or idprem or bodyprem or (stars is not None and stars >= 4))
    operator_leverage = bool(portfolio or portfolio_urls)
    visual_evidence = len(visual) >= 2

    selected = False
    if not bad:
        if pbrands:
            selected = score >= 50
        elif stars is not None and stars >= 5:
            # Five-star alone is useful, but require at least one corroborating
            # premium/visual/operator signal to avoid bad star metadata.
            selected = score >= 55 and bool(idprem or bodyprem or operator_leverage or visual_evidence)
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


if __name__ == "__main__":
    v4.evaluate = evaluate
    raise SystemExit(v4.main())
