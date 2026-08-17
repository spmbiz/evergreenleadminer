#!/usr/bin/env python3
"""Current-operation-safe wrapper for the source/ownership-safe GWS verifier.

This layer may only WITHHOLD a technical HIGH. It can never create HIGH or REJECT.
A no-website certificate is not enough if the POI/business identity itself may be stale.
Before a source-safe HIGH survives, require independent current-operation corroboration
from current search results and reject/withhold address-occupancy ambiguity.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import re
import unicodedata
from typing import Any

import gws_search_verify_source_safe as source

_ORIGINAL_CLASSIFY = source.classify_strict_source_safe

_CURRENT_TERMS = {
    "2025", "2026", "open", "opened", "closed", "hours", "opening", "today",
    "ouvert", "ouverte", "ferme", "fermee", "horaires", "horaire", "aujourd",
    "reservation", "reserver", "booking", "book", "reserve", "reservatie",
    "maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
}
_NAME_STOP = {
    "the", "de", "la", "le", "les", "du", "des", "and", "et", "a", "au", "aux",
    "srl", "sprl", "bv", "sa", "nv", "belgium", "belgique", "brussels", "bruxelles",
}
_ADDRESS_STOP = {
    "rue", "avenue", "av", "chaussée", "chaussee", "steenweg", "place", "plein",
    "boulevard", "laan", "street", "road", "square", "brussels", "bruxelles", "belgium",
    "belgique", "be",
}


def _ascii(value: Any) -> str:
    return unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode().lower()


def _tokens(value: Any) -> list[str]:
    return re.findall(r"[a-z0-9]+", _ascii(value))


def _compact(value: Any) -> str:
    return "".join(_tokens(value))


def _digits(value: Any) -> str:
    return re.sub(r"\D+", "", str(value or ""))


def _host(url: str) -> str:
    return source.safe.base.v2.host(str(url or ""))


def _name_tokens(name: str) -> list[str]:
    return [t for t in _tokens(name) if len(t) >= 3 and t not in _NAME_STOP]


def _address_parts(address: str) -> tuple[str, list[str]]:
    toks = _tokens(address)
    number = next((t for t in toks if t.isdigit()), "")
    words = [t for t in toks if len(t) >= 4 and not t.isdigit() and t not in _ADDRESS_STOP]
    return number, words[:4]


def _name_match(name: str, text: str) -> bool:
    compact_name = _compact(name)
    compact_text = _compact(text)
    if len(compact_name) >= 5 and compact_name in compact_text:
        return True
    nt = _name_tokens(name)
    if not nt:
        return False
    hits = sum(1 for t in nt if t in _tokens(text))
    need = 1 if len(nt) == 1 else max(2, math.ceil(len(nt) * 0.6))
    return hits >= need


def _address_match(address: str, text: str) -> bool:
    number, words = _address_parts(address)
    toks = set(_tokens(text))
    if number and number not in toks:
        return False
    if not words:
        return bool(number)
    return sum(1 for w in words if w in toks) >= 1


def _phone_match(phone: str, text: str) -> bool:
    p = _digits(phone)
    if len(p) < 7:
        return False
    t = _digits(text)
    return p[-8:] in t or p[-7:] in t


def _current_signal(text: str) -> bool:
    toks = set(_tokens(text))
    if toks & _CURRENT_TERMS:
        return True
    # Common current-hours syntax survives snippet truncation.
    s = _ascii(text)
    return bool(re.search(r"\b\d{1,2}[:h]\d{2}\b", s))


def current_operation_challenge(row: dict[str, Any], c: dict[str, Any], fabric) -> dict[str, Any]:
    name = str(c.get("n") or row.get("hub_name") or row.get("overture_name") or "").strip()
    address = str(c.get("a") or row.get("hub_address") or row.get("overture_address") or "").strip()
    phone = str(c.get("ph") or row.get("overture_phone") or row.get("hub_phone") or "").strip()
    operating_status = str(row.get("overture_operating_status") or "").strip().upper()

    queries: list[tuple[str, str]] = []
    if name and address:
        queries.append(("identity_current", f'"{name}" "{address}" 2026'))
        queries.append(("identity_hours", f'"{name}" "{address}" horaires'))
        queries.append(("address_occupancy", f'"{address}" 2026'))
    if name and phone:
        queries.insert(1, ("identity_phone", f'"{name}" "{phone}"'))
    queries = queries[:4]

    events: list[dict[str, Any]] = []
    corroborating: dict[str, dict[str, Any]] = {}
    competing: dict[str, dict[str, Any]] = {}
    usable_queries = 0

    for family, query in queries:
        results, event = fabric._openserp("current_operation_challenge", query)
        meta = event.get("meta") or {}
        fams = source.safe.base.provider_families(meta) if event.get("status") == "OK" else set()
        if fams:
            usable_queries += 1
        qevent = {
            "family": family,
            "query": query,
            "status": event.get("status"),
            "families": sorted(fams),
            "results": len(results),
            "error": event.get("error"),
        }
        events.append(qevent)
        for item in results:
            url = str(getattr(item, "url", "") or "")
            host = _host(url)
            if not host:
                continue
            text = " ".join([
                str(getattr(item, "title", "") or ""),
                str(getattr(item, "snippet", "") or ""),
                url,
            ])
            nm = _name_match(name, text)
            am = _address_match(address, text) if address else False
            pm = _phone_match(phone, text) if phone else False
            current = _current_signal(text)
            identity = bool(nm and (am or pm))
            evidence = {
                "host": host,
                "url": url,
                "query_family": family,
                "name_match": nm,
                "address_match": am,
                "phone_match": pm,
                "current_signal": current,
                "title": str(getattr(item, "title", "") or "")[:200],
                "snippet": str(getattr(item, "snippet", "") or "")[:400],
            }
            if identity:
                previous = corroborating.get(host)
                if previous is None or (current and not previous.get("current_signal")):
                    corroborating[host] = evidence
            elif family == "address_occupancy" and am and current and not nm:
                # A current-looking different identity at the exact address is not a
                # deterministic REJECT, but it is sufficient to withhold HIGH.
                competing.setdefault(host, evidence)

    evidence_rows = list(corroborating.values())
    current_hosts = {x["host"] for x in evidence_rows if x.get("current_signal")}
    all_hosts = {x["host"] for x in evidence_rows}
    competing_hosts = set(competing)

    source_active = operating_status in {"OPEN", "ACTIVE", "OPERATING", "OPERATIONAL"}
    enough_independent = len(all_hosts) >= 2 and (bool(current_hosts) or source_active)

    if usable_queries == 0:
        status = "SEARCH_INCOMPLETE"
    elif competing_hosts and not enough_independent:
        status = "CURRENT_OPERATOR_AMBIGUOUS"
    elif enough_independent:
        status = "CORROBORATED"
    else:
        status = "NOT_CORROBORATED"

    return {
        "status": status,
        "usable_queries": usable_queries,
        "source_operating_status": operating_status,
        "source_active": source_active,
        "corroborating_hosts": sorted(all_hosts),
        "current_signal_hosts": sorted(current_hosts),
        "competing_hosts": sorted(competing_hosts),
        "corroborating_evidence": evidence_rows[:8],
        "competing_evidence": list(competing.values())[:5],
        "queries": events,
    }


def classify_strict_current_safe(row: dict[str, Any], c: dict[str, Any], pe: dict[str, Any], fabric, max_queries: int) -> dict[str, Any]:
    out = _ORIGINAL_CLASSIFY(row, c, pe, fabric, max_queries)
    if str(out.get("verification_status") or "").upper() != "HIGH":
        return out

    challenge = current_operation_challenge(row, c, fabric)
    out["current_operation_challenge"] = challenge
    cert = deepcopy(out.get("certificate") or {})
    cert["current_operation_challenge"] = challenge

    if challenge.get("status") == "CORROBORATED":
        cert["current_operation_verified"] = True
        old_digest = str(cert.get("evidence_digest") or out.get("certificate_digest") or "")
        payload = json.dumps({"previous": old_digest, "current_operation": challenge}, ensure_ascii=False, sort_keys=True, default=str)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        cert["pre_current_operation_evidence_digest"] = old_digest
        cert["evidence_digest"] = digest
        out["certificate"] = cert
        out["certificate_digest"] = digest
        return out

    if challenge.get("status") == "SEARCH_INCOMPLETE":
        out.update({
            "outcome": "REVIEW",
            "reason": "CURRENT_OPERATION_CHALLENGE_SEARCH_INCOMPLETE",
            "verification_status": "ERROR_RETRYABLE",
            "needs_gpt_review": True,
            "owned_website": "",
        })
    elif challenge.get("status") == "CURRENT_OPERATOR_AMBIGUOUS":
        out.update({
            "outcome": "UNCERTAIN",
            "reason": "CURRENT_OPERATOR_AT_ADDRESS_AMBIGUOUS",
            "verification_status": "UNCERTAIN",
            "needs_gpt_review": True,
            "owned_website": "",
        })
    else:
        out.update({
            "outcome": "UNCERTAIN",
            "reason": "CURRENT_OPERATION_NOT_INDEPENDENTLY_CORROBORATED",
            "verification_status": "UNCERTAIN",
            "needs_gpt_review": True,
            "owned_website": "",
        })
    cert["verified"] = False
    cert["current_operation_verified"] = False
    cert["superseded_no_website_digest"] = str(cert.get("evidence_digest") or out.get("certificate_digest") or "")
    out["certificate"] = cert
    out["certificate_digest"] = ""
    return out


def main() -> int:
    source.safe.classify_strict_safe = classify_strict_current_safe
    return source.safe.main()


if __name__ == "__main__":
    raise SystemExit(main())
