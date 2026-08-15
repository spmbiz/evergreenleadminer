#!/usr/bin/env python3
"""Conservative current-identity resolver hardening for Brussels GWS.

The legacy resolver compared digit strings literally, so a Belgian local phone
such as 02 521 58 59 did not equal the same E.164 phone +32 2 521 58 59.
This module canonicalizes equivalent Belgian national/international forms while
preserving the existing name/address/postcode thresholds. It does not introduce
fuzzy phone matching or suffix-only matching.
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
    # Normalize international access prefix.
    if d.startswith("0032"):
        d = d[2:]
    out = {d}
    if d.startswith("32") and len(d) >= 10:
        # +32 removes the Belgian trunk zero.
        national = "0" + d[2:]
        out.add(national)
    elif d.startswith("0") and len(d) >= 9:
        out.add("32" + d[1:])
    # Keep only plausible full Belgian representations; never compare mere suffixes.
    return {x for x in out if 9 <= len(x) <= 11}


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
        return None, {"resolved": False, "phone_normalization": "be-national-e164-v1"}
    _, p, px, ns, ao, pm, matched_phone_keys = best
    # Same non-phone identity thresholds as the legacy resolver. The only
    # behavior change is recognizing equivalent full Belgian phone forms.
    ok = px or (ns >= 0.91 and (pm or ao >= 0.2)) or (ns >= 0.82 and pm and ao >= 0.25)
    ev = {
        "resolved": ok,
        "phone_exact": px,
        "phone_matched_keys": matched_phone_keys,
        "phone_normalization": "be-national-e164-v1",
        "name_similarity": round(ns, 3),
        "address_overlap": round(ao, 3),
        "postcode_match": pm,
        "overture_id": v2.t(p.get("id")),
        "overture_name": v2.t(p.get("name")),
        "operating_status": v2.t(p.get("operating_status")),
    }
    return (p if ok else None), ev
