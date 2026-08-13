#!/usr/bin/env python3
from __future__ import annotations
import csv, json, math, re, time, unicodedata, urllib.parse, urllib.request
from collections import defaultdict, Counter
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlparse
import duckdb

DATASET="commerces-recenses-par-hubbrussels-vbx"
HUB=f"https://opendata.brussels.be/api/explore/v2.1/catalog/datasets/{DATASET}/records"
RELEASE="2026-06-17.0"
OVERTURE=f"s3://overturemaps-us-west-2/release/{RELEASE}/theme=places/type=place/*"

TARGET_KWS=(
 "hairdresser","barber","beauty salon","aesthetic","nails","manicure","pedicure",
 "garage","car maintenance","auto repair","bodywork","tyres","tires",
 "mobile phones","telephone booths","telecom","laundromat","laundry","dry cleaning",
 "bakery","butcher","charcuterie","shoe repair","locksmith","tailor","photo studio",
 "printing","printer","florist","jewelry repair","watch repair","pet grooming",
)
CHAIN_WORDS=(
 "carrefour","delhaize","lidl","aldi","action","kruidvat","ici paris","di beauty",
 "basic-fit","orange","proximus","base shop","telenet","mediamarkt","quick",
 "mcdonald","burger king","starbucks","pizza hut","domino","panos","exki",
 "fintro","bnp paribas","belfius","ing","kbc","crelan",
)
PLATFORM_DOMAINS=(
 "facebook.com","instagram.com","tiktok.com","linkedin.com","youtube.com",
 "treatwell.be","mytreatwell.be","planity.com","fresha.com","salonkee.be",
 "pagesdor.be","goudengids.be","bizique.be","cylex-belgie.be","opendi.be",
 "bolid.be","garagebelgique.com","selfcity.be","heures.be","openingsuren.vlaanderen",
 "tripadvisor.","yelp.","google.","maps.apple.","waze.com","ubereats.com",
 "deliveroo.","takeaway.com","booking.com",
)

def txt(v):
    if v is None: return ""
    return re.sub(r"\s+"," ",str(v)).strip()

def norm(s):
    s=unicodedata.normalize("NFKD",txt(s)).encode("ascii","ignore").decode().lower()
    s=re.sub(r"[^a-z0-9]+"," ",s)
    return re.sub(r"\s+"," ",s).strip()

def tokens(s):
    return {x for x in norm(s).split() if len(x)>1 and x not in {"the","de","la","le","les","du","des","and","et","sa","sprl","srl","bv","nv"}}

def sim(a,b):
    a,b=norm(a),norm(b)
    if not a or not b:return 0.0
    seq=SequenceMatcher(None,a,b).ratio()
    ta,tb=tokens(a),tokens(b)
    jac=len(ta&tb)/max(1,len(ta|tb))
    contains=1.0 if (a in b or b in a) and min(len(a),len(b))>=4 else 0.0
    return max(seq, 0.65*seq+0.35*jac, 0.72*contains+0.28*jac)

def hav(lat1,lon1,lat2,lon2):
    R=6371000.0
    p1,p2=math.radians(lat1),math.radians(lat2)
    dp=math.radians(lat2-lat1); dl=math.radians(lon2-lon1)
    a=math.sin(dp/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*R*math.asin(min(1,math.sqrt(a)))

def first(v):
    if isinstance(v,(list,tuple)): return txt(v[0]) if v else ""
    return txt(v)

def host(u):
    try:
        h=(urlparse(u).hostname or "").lower().strip(".")
        return h[4:] if h.startswith("www.") else h
    except:return ""

def is_platform(h):
    return any(x in h for x in PLATFORM_DOMAINS)

def owned_site(websites):
    if not websites:return ""
    vals=websites if isinstance(websites,(list,tuple)) else [websites]
    for u in vals:
        u=txt(u); h=host(u)
        if h and not is_platform(h):
            return u
    return ""

def brand_name(v):
    if not isinstance(v,dict): return ""
    n=v.get("names")
    if isinstance(n,dict): return txt(n.get("primary"))
    return txt(v.get("name"))

def fetch_hub():
    out=[]; offset=0; total=None; pages=[]
    while total is None or offset<total:
        q=urllib.parse.urlencode({"limit":100,"offset":offset})
        req=urllib.request.Request(HUB+"?"+q,headers={"User-Agent":"GWS-Brussels-Resolver-V2/1.0","Accept":"application/json"})
        with urllib.request.urlopen(req,timeout=45) as r: data=json.load(r)
        if total is None: total=int(data["total_count"])
        batch=data.get("results",[])
        if not batch: raise RuntimeError(f"empty hub page {offset}/{total}")
        out.extend(batch);pages.append(len(batch));offset+=len(batch)
    if len(out)!=total: raise RuntimeError((len(out),total))
    return out,total,pages

def target_row(r):
    n=norm(r.get("name_en") or r.get("name_fr"))
    typ=norm(r.get("type_en") or r.get("type_fr"))
    cat=norm(r.get("category_en") or r.get("category_fr"))
    if not n or n=="-" or "empty commercial cell" in typ:return False
    # Scope by the official business TYPE, never by words in the business name.
    # This prevents false positives such as a pub named "The Hairy Canary".
    if any(c in n for c in CHAIN_WORDS): return False
    return any(k in typ for k in TARGET_KWS)

def gp(r):
    g=r.get("geo_point_2d") or {}
    return float(g.get("lat")),float(g.get("lon"))

def main():
    out=Path("results/gws_brussels_resolver_v2");out.mkdir(parents=True,exist_ok=True)
    t0=time.time()
    hub,total,pages=fetch_hub()
    target=[r for r in hub if target_row(r)]
    lats=[];lons=[]
    for r in hub:
        try:
            a,b=gp(r);lats.append(a);lons.append(b)
        except:pass
    minlat,maxlat=min(lats)-0.015,max(lats)+0.015
    minlon,maxlon=min(lons)-0.02,max(lons)+0.02

    con=duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2';")
    q=f"""
    SELECT id,names.primary AS name,basic_category,categories.primary AS category_primary,
           websites,socials,emails,phones,brand,addresses,confidence,operating_status,
           (bbox.xmin+bbox.xmax)/2.0 AS longitude,(bbox.ymin+bbox.ymax)/2.0 AS latitude
    FROM read_parquet('{OVERTURE}', hive_partitioning=1)
    WHERE bbox.xmax >= {minlon} AND bbox.xmin <= {maxlon}
      AND bbox.ymax >= {minlat} AND bbox.ymin <= {maxlat}
      AND (operating_status IS NULL OR operating_status='open')
      AND names.primary IS NOT NULL
    """
    cur=con.execute(q); cols=[d[0] for d in cur.description]
    places=[dict(zip(cols,x)) for x in cur.fetchall()]

    scale=1000
    grid=defaultdict(list)
    for p in places:
        try: key=(int(float(p["latitude"])*scale),int(float(p["longitude"])*scale));grid[key].append(p)
        except:pass

    rows=[]
    for h in target:
        try: lat,lon=gp(h)
        except: continue
        name=txt(h.get("name_en") or h.get("name_fr"))
        key=(int(lat*scale),int(lon*scale))
        best=None
        for di in range(-2,3):
            for dj in range(-2,3):
                for p in grid.get((key[0]+di,key[1]+dj),[]):
                    d=hav(lat,lon,float(p["latitude"]),float(p["longitude"]))
                    if d>120: continue
                    ns=sim(name,p.get("name"))
                    score=ns*100 - min(d,100)*0.18 + float(p.get("confidence") or 0)*8
                    if best is None or score>best[0]: best=(score,d,ns,p)
        resolved=False; p=None; dist=None; ns=0
        if best:
            _,dist,ns,p=best
            resolved=((dist<=25 and ns>=0.72) or (dist<=12 and ns>=0.55) or (dist<=50 and ns>=0.90))
        site=owned_site(p.get("websites")) if resolved else ""
        brand=brand_name(p.get("brand")) if resolved else ""
        chain=bool(brand and any(c in norm(brand) for c in CHAIN_WORDS))
        prelim="UNCERTAIN"
        reason="NO_EXACT_CURRENT_PLACE_MATCH"
        if resolved and (site or chain):
            prelim="REJECT"; reason="OWNED_SITE_FOUND" if site else "CHAIN_BRAND"
        elif resolved:
            prelim="MEDIUM"; reason="CURRENT_ENTITY_RESOLVED_NO_OWNED_SITE_IN_OVERTURE"
        rows.append({
          "hub_objectid":txt(h.get("objectid")),"hub_name":name,"hub_type":txt(h.get("type_en")),
          "hub_category":txt(h.get("category_en")),"hub_address":txt(h.get("address_fr")),
          "hub_postalcode":txt(h.get("postalcode")),"hub_google_maps":txt(h.get("google_maps")),
          "hub_lat":lat,"hub_lon":lon,"overture_id":txt(p.get("id")) if resolved else "",
          "overture_name":txt(p.get("name")) if resolved else "","distance_m":round(dist,1) if resolved else "",
          "name_similarity":round(ns,3) if resolved else "","overture_confidence":txt(p.get("confidence")) if resolved else "",
          "overture_category":txt(p.get("category_primary") or p.get("basic_category")) if resolved else "",
          "overture_phone":first(p.get("phones")) if resolved else "","overture_email":first(p.get("emails")) if resolved else "",
          "overture_websites":json.dumps(p.get("websites"),ensure_ascii=False,default=str) if resolved and p.get("websites") else "",
          "owned_website":site,"overture_socials":json.dumps(p.get("socials"),ensure_ascii=False,default=str) if resolved and p.get("socials") else "",
          "overture_brand":brand,"preliminary_outcome":prelim,"reason":reason,
        })

    exact=[r for r in rows if r["preliminary_outcome"]=="MEDIUM"]
    exact.sort(key=lambda r:(float(r["overture_confidence"] or 0), -float(r["distance_m"] or 999), float(r["name_similarity"] or 0)),reverse=True)
    uncertain=[r for r in rows if r["preliminary_outcome"]=="UNCERTAIN"]
    uncertain.sort(key=lambda r:(float(r["name_similarity"] or 0),-float(r["distance_m"] or 999)),reverse=True)
    rejects=[r for r in rows if r["preliminary_outcome"]=="REJECT"]
    serious=(exact+uncertain+rejects)[:500]
    for i,r in enumerate(serious,1): r["serious_rank"]=i
    webgate=exact[:150]
    for i,r in enumerate(webgate,1): r["web_gate_rank"]=i

    fields=list(rows[0].keys()) if rows else []
    for fn,data,extra in [
        ("all_target_matches.csv",rows,[]),
        ("serious500.csv",serious,["serious_rank"]),
        ("web_gate_top150.csv",webgate,["web_gate_rank"]),
    ]:
        flds=extra+[x for x in fields if x not in extra]
        with (out/fn).open("w",encoding="utf-8",newline="") as f:
            w=csv.DictWriter(f,fieldnames=flds,extrasaction="ignore");w.writeheader();w.writerows(data)
    summary={
      "hub_api_total":total,"hub_materialized":len(hub),"hub_pages":len(pages),
      "target_independent_lane":len(target),"overture_release":RELEASE,"overture_places_in_bbox":len(places),
      "exact_current_matches":sum(r["preliminary_outcome"]!="UNCERTAIN" for r in rows),
      "exact_no_owned_site":len(exact),"owned_site_or_chain_rejects":len(rejects),"unresolved":len(uncertain),
      "serious_attempts_materialized":len(serious),
      "serious_preliminary":dict(Counter(r["preliminary_outcome"] for r in serious)),
      "web_gate_candidates":len(webgate),"elapsed_seconds":round(time.time()-t0,2),
      "bbox":[minlon,minlat,maxlon,maxlat]
    }
    (out/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
    print("GWS_RESOLVER_V2_SUMMARY="+json.dumps(summary,ensure_ascii=False))

if __name__=="__main__": main()
