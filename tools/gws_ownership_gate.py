#!/usr/bin/env python3
from __future__ import annotations

import re
from urllib.parse import urlparse
from typing import Any

# Third-party/editorial/directory/marketplace hosts are evidence about the business,
# not websites owned by the business. Keep this list intentionally conservative.
THIRD_PARTY_SUFFIXES = {
    "information-bruxelles.be", "belgiumaps.com", "hotfrogbe.be", "annuaire-horaire.be",
    "boucheriebelgique.com", "data.be", "belgoptic.be", "koifaire.com", "obodo.be",
    "dontpayfull.com", "city2.be", "microkinebelgique.be", "primegrapewinery.com",
    "findyourhairstylist.com", "wasgij.com", "gundam-official.com", "marinelink.com",
    "library.ucsb.edu", "cybo.com", "top10place.com", "belgiumyp.com", "revieweuro.com",
    "tartine-et-boterham.be", "companyweb.be", "pagesdor.be", "goudengids.be",
    "fresha.com", "treatwell.be", "treatwell.com", "yelp.com", "tripadvisor.com",
    "facebook.com", "instagram.com", "linkedin.com", "tiktok.com", "youtube.com",
    "booking.com", "mapstr.com", "waze.com", "google.com", "bing.com", "duckduckgo.com",
    "amazon.com", "walmart.com", "target.com", "fandom.com", "wikipedia.org",
}

GENERIC_NAME_TOKENS = {
    "the", "de", "la", "le", "les", "du", "des", "et", "and", "a", "au", "aux",
    "srl", "sprl", "bv", "nv", "sa", "belgium", "belgique", "bruxelles", "brussels",
    "salon", "coiffure", "beauty", "garage", "mobile", "gsm", "boucherie", "boulangerie",
    "hair", "healthy", "massage", "optique", "vision", "clinic", "shop", "store",
}


def _host(url: str) -> str:
    try:
        h = (urlparse(str(url or "")).hostname or "").lower().strip(".")
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""


def _compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _tokens(value: str) -> list[str]:
    return [x for x in re.findall(r"[a-z0-9]+", str(value or "").lower()) if len(x) >= 3 and x not in GENERIC_NAME_TOKENS]


def is_third_party(url: str) -> bool:
    h = _host(url)
    if not h:
        return True
    return any(h == d or h.endswith("." + d) for d in THIRD_PARTY_SUFFIXES)


def assess(row: dict[str, Any], pass_ev: dict[str, Any] | None) -> dict[str, Any]:
    p = pass_ev or {}
    url = str(p.get("owned") or "")
    ident = p.get("owned_identity") or {}
    h = _host(url)
    name = str(row.get("hub_name") or (row.get("candidate") or {}).get("n") or "")

    domain_overlap = float(ident.get("domain_name_overlap") or 0.0)
    address_overlap = float(ident.get("address_overlap") or 0.0)
    phone_exact = bool(ident.get("phone"))
    postcode = bool(ident.get("postcode"))

    name_compact = _compact(name)
    host_compact = _compact(h.split(".")[0] if h else "")
    meaningful = _tokens(name)
    token_hits = sum(1 for t in meaningful if t in host_compact)
    token_ratio = token_hits / max(1, len(meaningful))
    compact_brand_match = bool(name_compact and len(name_compact) >= 5 and name_compact in _compact(h))
    branded_host = bool(domain_overlap >= 0.50 or compact_brand_match or token_ratio >= 0.75)

    third_party = is_third_party(url)
    # Identity on a page is not ownership. Require a branded host plus a very
    # strong first-party signal. Postcode or partial street overlap is insufficient:
    # directories and unrelated homonyms routinely satisfy those weaker signals.
    strong_first_party_identity = bool(phone_exact or address_overlap >= 0.75)
    confident = bool(url and not third_party and branded_host and strong_first_party_identity)

    if not url:
        reason = "NO_OWNED_CANDIDATE"
    elif third_party:
        reason = "KNOWN_THIRD_PARTY_HOST"
    elif not branded_host:
        reason = "HOST_NOT_BRANDED_TO_BUSINESS"
    elif not strong_first_party_identity:
        reason = "BRANDED_HOST_IDENTITY_NOT_STRONG_ENOUGH"
    else:
        reason = "OWNERSHIP_CONFIRMED"

    return {
        "confident": confident,
        "reason": reason,
        "url": url,
        "host": h,
        "third_party": third_party,
        "branded_host": branded_host,
        "compact_brand_match": compact_brand_match,
        "meaningful_name_tokens": meaningful,
        "domain_name_overlap": round(domain_overlap, 3),
        "address_overlap": round(address_overlap, 3),
        "phone_exact": phone_exact,
        "postcode_match": postcode,
        "host_token_ratio": round(token_ratio, 3),
    }
