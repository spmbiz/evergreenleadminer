#!/usr/bin/env python3
"""Deterministic reference/directory helpers for GWS.

Reference pages are useful discovery surfaces but never count as an owned site.
Their outbound links may become owned-site candidates only after independent HTTP
identity validation. Direct brand-domain probes try a small set of realistic
landing variants before declaring a host dead.
"""
from __future__ import annotations

import html
import re
import urllib.parse

REFERENCE_MARKERS=(
    "pagesdor.","goudengids.","bizique.","cylex.","opendi.","openingsuren.",
    "openingsuren.vlaanderen","heures.","hours.","selfcity.","companyweb.",
    "infobel.","bottin.","atout-commerces.","lokal-handel.","pappers.",
    "openthebox.","garagebelgique.","nosavis.","idgarages.","mappy.",
    "autoscout24.","brusselslife.","findglocal.","proxibel.","bsearch.",
    "kompass.","firmania.","furniture1000.","wanderlog.","busibee.",
    "creditsafe.","numero-pro.",
)


def host(url):
    try:
        h=(urllib.parse.urlparse(url if "://" in str(url) else "https://"+str(url)).hostname or "").lower().strip('.')
        return h[4:] if h.startswith('www.') else h
    except Exception:
        return ""


def is_reference(url):
    h=host(url); return bool(h and any(x in h for x in REFERENCE_MARKERS))


def direct_variants(url):
    """Bounded landing variants for one canonical host.

    `/index.html` catches a surprising class of small legacy business sites whose
    root returns a default 404. `www` and http are fallbacks, not extra domains.
    """
    h=host(url)
    if not h: return []
    out=[]
    for u in (
        f"https://{h}/",
        f"https://{h}/index.html",
        f"https://www.{h}/",
        f"https://www.{h}/index.html",
        f"http://{h}/",
        f"http://www.{h}/",
    ):
        if u not in out: out.append(u)
    return out


def listing_identity(v2,c,body):
    """Conservative identity test for a directory/reference listing page."""
    tx=v2.textish(body)
    name=max(v2.ov(c.get('n'),tx),v2.ov(c.get('alias'),tx) if c.get('alias') else 0)
    addr=v2.ov(c.get('a'),tx)
    pc=v2.t(c.get('p'))[:4]; pcm=bool(pc and pc in tx)
    ph=re.sub(r"\D+","",v2.t(c.get('ph')))
    digits=re.sub(r"\D+","",tx)
    phone=bool(ph and len(ph)>=8 and ph[-8:] in digits)
    ok=(phone and (name>=.30 or addr>=.22 or pcm)) or (name>=.72 and (addr>=.22 or pcm))
    return {"matched":ok,"name_overlap":round(name,3),"address_overlap":round(addr,3),"postcode_match":pcm,"phone_match":phone}


def outbound_candidates(v2,body,base,limit=12):
    out=[]; seen=set()
    for raw in re.findall(r'''href\s*=\s*["']([^"'#]+)''',body or "",re.I):
        u=html.unescape(urllib.parse.urljoin(base,raw.strip()))
        if not u.startswith('http'): continue
        h=host(u)
        if not h or h in seen or is_reference(u) or v2.platform(u): continue
        seen.add(h); out.append(u)
        if len(out)>=limit: break
    return out
