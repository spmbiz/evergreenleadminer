#!/usr/bin/env python3
"""Thin production wrapper around gws_fleet_worker.

It preserves the Overture address/postcode/phone identity evidence that the strict
no-website certificate needs, without changing discovery or ranking semantics.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict

import gws_fleet_worker as base


# The legacy worker still defaults to a dated Overture release. Keep explicit
# --release pins working, but resolve the normal/default path through the same
# hardened STAC resolver used by the strict v5.3 certifier.
base.DEFAULT_RELEASE = "latest"
_base_query_overture = base.query_overture
OFFICIAL_STAC_FALLBACK_RELEASE = "2026-07-22.0"


def query_overture_current(targets: list[dict], release: str, threads: int):
    requested=str(release or "").strip()
    if not requested or requested.casefold() == "latest":
        from gws_no_website_certifier_v53_core import resolve_overture_release
        try:
            requested=resolve_overture_release()
        except RuntimeError as exc:
            if str(exc).startswith("OVERTURE_STAC_UNAVAILABLE:"):
                requested=OFFICIAL_STAC_FALLBACK_RELEASE
            else:
                raise
    return _base_query_overture(targets, requested, threads)


base.query_overture = query_overture_current


def norm_phone(v) -> str:
    d=re.sub(r"\D+","",base.txt(v))
    if d.startswith("0032"): d="32"+d[4:]
    if d.startswith("0") and len(d)>=9: d="32"+d[1:]
    return d


def place_address(v) -> str:
    x=(v[0] if isinstance(v,(list,tuple)) and v else v)
    if isinstance(x,dict):
        vals=[]
        for k in ("freeform","street","house_number","postcode","locality","region","country"):
            val=x.get(k)
            if val: vals.append(base.txt(val))
        if vals: return " ".join(vals)
        return base.txt(json.dumps(x,ensure_ascii=False,default=str))
    return base.txt(x)


def resolve_enriched(targets: list[dict], places: list[dict], max_serious: int) -> list[dict]:
    scale=1000
    grid: dict[tuple[int,int],list[dict]]=defaultdict(list)
    for p in places:
        try:
            key=(int(float(p["latitude"])*scale),int(float(p["longitude"])*scale)); grid[key].append(p)
        except Exception:
            continue
    rows=[]
    for h in targets:
        try: lat,lon=base.geo(h)
        except Exception: continue
        name=base.business_name(h); key=(int(lat*scale),int(lon*scale)); best=None
        for di in range(-2,3):
            for dj in range(-2,3):
                for p in grid.get((key[0]+di,key[1]+dj),[]):
                    d=base.hav(lat,lon,float(p["latitude"]),float(p["longitude"]))
                    if d>120: continue
                    ns=base.sim(name,p.get("name")); score=ns*100-min(d,100)*0.18+float(p.get("confidence") or 0)*8
                    if best is None or score>best[0]: best=(score,d,ns,p)
        resolved=False; p=None; dist=None; ns=0.0
        if best:
            _,dist,ns,p=best
            resolved=((dist<=25 and ns>=0.72) or (dist<=12 and ns>=0.55) or (dist<=50 and ns>=0.90))

        site=base.owned_site(p.get("websites")) if resolved else ""
        brand=base.brand_name(p.get("brand")) if resolved else ""
        chain=bool(brand and any(c in base.norm(brand) for c in base.CHAIN_WORDS))
        if resolved and (site or chain): outcome,reason,needs_review="REJECT",("OWNED_SITE_FOUND" if site else "CHAIN_BRAND"),False
        elif resolved: outcome,reason,needs_review="REVIEW","CURRENT_ENTITY_RESOLVED_NO_OWNED_SITE_IN_OVERTURE",True
        else: outcome,reason,needs_review="UNCERTAIN","NO_EXACT_CURRENT_PLACE_MATCH",True

        hub_addr=base.business_address(h); hub_pc=base.postal_code(h)
        hub_phone=base.txt(h.get("phone") or h.get("telephone") or h.get("tel") or h.get("phone_number"))
        hub_email=base.txt(h.get("email") or h.get("mail"))
        ov_addr=place_address(p.get("addresses")) if resolved else ""
        hub_tokens=base.tokens(hub_addr); ov_tokens=base.tokens(ov_addr)
        addr_overlap=len(hub_tokens&ov_tokens)/max(1,len(hub_tokens)) if hub_tokens else 0.0
        raw_addresses=json.dumps(p.get("addresses"),ensure_ascii=False,default=str) if resolved and p.get("addresses") else ""
        postcode_match=bool(resolved and hub_pc and re.search(r"(?<!\d)"+re.escape(hub_pc)+r"(?!\d)",raw_addresses))
        ov_phone=base.first(p.get("phones")) if resolved else ""
        phone_exact=bool(hub_phone and ov_phone and norm_phone(hub_phone)==norm_phone(ov_phone))

        row={
            "hub_objectid":base.hub_id(h),"hub_name":name,"hub_type":base.business_type(h),"hub_category":base.business_category(h),
            "hub_address":hub_addr,"hub_postalcode":hub_pc,"hub_phone":hub_phone,"hub_email":hub_email,
            "hub_google_maps":base.txt(h.get("google_maps")),"hub_lat":lat,"hub_lon":lon,
            "overture_id":base.txt(p.get("id")) if resolved else "","overture_name":base.txt(p.get("name")) if resolved else "",
            "overture_resolved":bool(resolved),"distance_m":round(float(dist),1) if resolved and dist is not None else "",
            "name_similarity":round(ns,3) if resolved else "","overture_confidence":base.txt(p.get("confidence")) if resolved else "",
            "overture_category":base.txt(p.get("category_primary") or p.get("basic_category")) if resolved else "",
            "overture_phone":ov_phone,"overture_email":base.first(p.get("emails")) if resolved else "",
            "overture_address":ov_addr,"overture_addresses":raw_addresses,"address_overlap":round(addr_overlap,3) if resolved else 0.0,
            "postcode_match":postcode_match,"phone_exact":phone_exact,"overture_operating_status":base.txt(p.get("operating_status")) if resolved else "",
            "overture_websites":json.dumps(p.get("websites"),ensure_ascii=False,default=str) if resolved and p.get("websites") else "",
            "owned_website":site,"overture_socials":json.dumps(p.get("socials"),ensure_ascii=False,default=str) if resolved and p.get("socials") else "",
            "overture_brand":brand,"outcome":outcome,"reason":reason,"needs_gpt_review":needs_review,
        }
        row["record_key"]=base.record_key(row); rows.append(row)

    rank={"REVIEW":0,"UNCERTAIN":1,"REJECT":2}
    rows.sort(key=lambda r:(rank.get(r["outcome"],9),-float(r["overture_confidence"] or 0),float(r["distance_m"] or 999),-float(r["name_similarity"] or 0)))
    serious=[]; review_count=0
    for r in rows:
        if r["outcome"] in {"REVIEW","UNCERTAIN"}:
            if review_count>=max_serious: continue
            review_count+=1
        serious.append(r)
    return serious


base.resolve=resolve_enriched

if __name__=="__main__":
    raise SystemExit(base.main())
