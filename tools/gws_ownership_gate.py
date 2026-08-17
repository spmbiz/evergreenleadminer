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
    "fresha.com", "treatwell.be", "treatwell.com", "planity.com", "yelp.com", "tripadvisor.com",
    "facebook.com", "instagram.com", "linkedin.com", "tiktok.com", "youtube.com",
    "booking.com", "mapstr.com", "waze.com", "google.com", "bing.com", "duckduckgo.com",
    "amazon.com", "walmart.com", "target.com", "fandom.com", "wikipedia.org",
    "ivof.com",
    # Observed Brussels false-positive ownership hosts. These are booking,
    # directory, association, editorial or business-index pages. They may prove
    # identity/current operation, but they never prove first-party website ownership.
    "medical-sante.be", "huisartsgids.be", "myconsultation.be", "rosa.be",
    "ssub.be", "bedrijvenwijzer.be", "combook.be", "doctoranytime.be",
    "restaurantguru.com", "restaurantguru.it", "restaurantguru.ru",
    "bizique.be", "epiceriebelgique.com", "allo-bruxelles.be", "nosavis.be",
    "cylex-belgie.be", "atout-commerces.be",
    # Red-team verified platform/directory hosts. Optios and Planity are salon
    # booking/software infrastructure; Hey Restaurants is restaurant discovery.
    # Client/profile pages on these hosts are never first-party sites.
    "optios.net", "hey-restaurants.com",
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


def _registrable_label(host: str) -> str:
    """Return the left-most registrable-looking brand label for ordinary domains.

    We do not use this as proof on its own. It is only a brand-shape signal that
    still requires exact phone or strong address identity later in assess().
    """
    h = str(host or "").lower().strip(".")
    if not h:
        return ""
    parts = [p for p in h.split(".") if p]
    if len(parts) >= 3 and parts[-2:] in (["co", "uk"], ["com", "au"], ["com", "br"]):
        return _compact(parts[-3])
    if len(parts) >= 2:
        return _compact(parts[-2])
    return _compact(parts[0])


def _distinctive_leading_brand_match(name: str, host: str) -> bool:
    """Catch brands such as OPTIL->optil.be and Herard's->herards.com safely.

    Only the first meaningful token is considered and it must be >=5 chars.
    A simple trailing possessive/plural ``s`` equivalence is allowed. Generic
    short labels such as ``bar`` therefore never become first-party evidence.
    """
    tokens = _tokens(name)
    if not tokens:
        return False
    lead = _compact(tokens[0])
    label = _registrable_label(host)
    if len(lead) < 5 or len(label) < 5:
        return False
    return lead == label or lead + "s" == label or label + "s" == lead


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
    leading_brand_match = _distinctive_leading_brand_match(name, h)
    branded_host = bool(domain_overlap >= 0.50 or compact_brand_match or leading_brand_match or token_ratio >= 0.75)

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
        "leading_brand_match": leading_brand_match,
        "meaningful_name_tokens": meaningful,
        "domain_name_overlap": round(domain_overlap, 3),
        "address_overlap": round(address_overlap, 3),
        "phone_exact": phone_exact,
        "postcode_match": postcode,
        "host_token_ratio": round(token_ratio, 3),
    }
