#!/usr/bin/env python3
"""Conservative current-identity resolver hardening for Brussels GWS.

Belgian national/E.164 phone forms are canonicalized, but an exact phone match is
not allowed to override a material entity mismatch. Phone reuse, shared premises,
service counters, recycled numbers, and stale directory records are common enough
that strict VERIFIED_NO_WEBSITE needs at least one corroborating identity signal.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict

import gws_legacy_deep_v2 as v2


def phone_keys(value) -> set[str]:
    d = re.sub(r"\D", "", v2.t(value))
    if not d:
        return set()
    if d.startswith("0032"):
        d = d[2:]
    out = {d}
    if d.startswith("32") and len(d) >= 10:
        out.add("0" + d[2:])
    elif d.startswith("0") and len(d) >= 9:
        out.add("32" + d[1:])
    return {x for x in out if 9 <= len(x) <= 11}


def phone_identity_corroborated(phone_exact: bool, name_similarity: float,
                                 address_overlap: float, postcode_match: bool) -> bool:
    """Fail closed when the phone points at a materially different current entity.

    This is intentionally stricter than the legacy resolver. A matching full phone
    is powerful evidence, but for strict HIGH eligibility it must be supported by
    the current name, or by a moderately matching name plus address/postcode.
    """
    if not phone_exact:
        return False
    ns = float(name_similarity or 0)
    ao = float(address_overlap or 0)
    pm = bool(postcode_match)
    if ns >= 0.55:
        return True
    if ns >= 0.42 and pm and ao >= 0.25:
        return True
    return False


def indexes(P):
    ph, ex, tk = defaultdict(list), defaultdict(list), defaultdict(list)
    for i, p in enumerate(P):
        ex[v2.n(p["name"])].append(i)
        for raw in p.get("phones") or []:
            for key in phone_keys(raw):
                ph[key].append(i)
        for x in list(v2.toks(p["name"]))[:4]:
            tk[x].append(i)
    return ph, ex, tk


def resolve(c, P, I):
    ph, ex, tk = I
    cphones = phone_keys(c.get("ph"))
    cn = v2.n(c.get("n"))
    ids = set(ex.get(cn, []))
    for key in cphones:
        ids.update(ph.get(key, []))
    for x in list(v2.toks(c.get("n")))[:4]:
        ids.update(tk.get(x, [])[:1200])
    best = None
    pc = v2.t(c.get("p"))[:4]
    for i in ids:
        p = P[i]
        pphones = set()
        for raw in p.get("phones") or []:
            pphones.update(phone_keys(raw))
        px = bool(cphones and (cphones & pphones))
        ns = v2.sim(c.get("n"), p.get("name"))
        ab = json.dumps(p.get("addresses"), ensure_ascii=False, default=str)
        ao = v2.ov(c.get("a"), ab)
        pm = bool(pc and pc in ab)
        sc = (1.6 if px else 0) + 0.8 * ns + 0.18 * ao + (0.12 if pm else 0)
        if best is None or sc > best[0]:
            best = (sc, p, px, ns, ao, pm, sorted(cphones & pphones))
    if not best:
        return None, {"resolved": False, "phone_normalization": "be-national-e164-v2-corroborated"}

    _, p, px, ns, ao, pm, matched_phone_keys = best
    phone_ok = phone_identity_corroborated(px, ns, ao, pm)
    non_phone_ok = (ns >= 0.91 and (pm or ao >= 0.2)) or (ns >= 0.82 and pm and ao >= 0.25)
    ok = bool(phone_ok or non_phone_ok)
    ev = {
        "resolved": ok,
        "phone_exact": px,
        "phone_corroborated": phone_ok,
        "phone_matched_keys": matched_phone_keys,
        "phone_normalization": "be-national-e164-v2-corroborated",
        "name_similarity": round(ns, 3),
        "address_overlap": round(ao, 3),
        "postcode_match": pm,
        "overture_id": v2.t(p.get("id")),
        "overture_name": v2.t(p.get("name")),
        "operating_status": v2.t(p.get("operating_status")),
    }
    return (p if ok else None), ev
