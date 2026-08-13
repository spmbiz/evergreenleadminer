#!/usr/bin/env python3
from __future__ import annotations
import csv,json,re,unicodedata
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse
import duckdb

RELEASE="2026-06-17.0"
OVERTURE=f"s3://overturemaps-us-west-2/release/{RELEASE}/theme=places/type=place/*"
# Wider Brussels-Capital envelope used by the previous resolver, with a small safety margin.
MINLON,MINLAT,MAXLON,MAXLAT=4.285,50.770,4.470,50.940

PLATFORMS=(
 "facebook.com","instagram.com","tiktok.com","linkedin.com","youtube.com","x.com","twitter.com",
 "treatwell.","planity.com","fresha.com","salonkee.","pagesdor.","goudengids.","bizique.",
 "cylex-","opendi.","selfcity.","heures.be","hours.be","openingsuren.","tripadvisor.","yelp.",
 "google.","waze.","ubereats.","deliveroo.","takeaway.","booking.com","ivof.com","localguide.",
)
CHAIN_WORDS=(
 "carrefour","delhaize","lidl","aldi","action","kruidvat","ici paris","di beauty","basic-fit",
 "orange","proximus","base shop","telenet","mediamarkt","quick","mcdonald","burger king","starbucks",
 "pizza hut","domino","panos","exki","bnp paribas","belfius","ing bank","kbc","crelan","shell",
 "totalenergies","q8","esso","ikea","h&m","zara","primark","decathlon","jysk","zeeman","brico",
 "hubo","gamma","hema","uniqlo","foot locker","jd sports","nike","adidas","louis delhaize",
)
BANNED_CATEGORY_KWS=(
 "school","university","college","kindergarten","library","government","ministry","embassy","consulate",
 "police","fire_station","courthouse","prison","park","playground","garden","monument","memorial","museum",
 "theatre","cinema","stadium","sports_field","tram_stop","bus_stop","railway","metro_station","parking",
 "apartment","residential","housing","cemetery","church","mosque","synagogue","temple","political",
 "association","non_profit","ngo","hospital","emergency_room","pharmacy","post_office","bank","atm",
)
POSITIVE_KWS=(
 # food / hospitality
 "restaurant","cafe","coffee","bakery","baker","butcher","deli","pastry","patisserie","bar","pub",
 "fast_food","ice_cream","catering","tea_house","sandwich","pizza","sushi","grill",
 # beauty / personal services
 "hair","barber","beauty","nail","spa","massage","tanning","tattoo","piercing","cosmetic","aesthetic",
 # repair / auto / local trades
 "auto_repair","car_repair","garage","body_shop","tire","tyre","car_wash","mechanic","bicycle_repair",
 "phone_repair","electronics_repair","computer_repair","appliance_repair","shoe_repair","watch_repair",
 "jewelry_repair","locksmith","tailor","alteration","laundry","laundromat","dry_clean","cleaning",
 "plumber","electrician","painter","roofer","carpenter","heating","hvac","contractor","construction",
 # retail/local specialists
 "florist","flower","jewelry","jewellery","optician","pet_groom","pet_store","veterinary","photo",
 "photograph","printer","printing","copy_shop","mobile_phone","telephone","telecom","furniture","interior",
 "real_estate","travel_agency","driving_school","fitness","gym","dance","music_school",
 # professional local services that can buy a site
 "accountant","lawyer","notary","insurance","consultant","architect","dentist","doctor","physiotherapy",
 "psychologist","chiropractor","clinic","medical_center",
)

def txt(v):
    if v is None:return ""
    return re.sub(r"\s+"," ",str(v)).strip()

def norm(s):
    s=unicodedata.normalize("NFKD",txt(s)).encode("ascii","ignore").decode().lower()
    s=re.sub(r"[^a-z0-9]+"," ",s)
    return re.sub(r"\s+"," ",s).strip()

def host(u):
    try:
        h=(urlparse(txt(u)).hostname or "").lower().strip(".")
        return h[4:] if h.startswith("www.") else h
    except:return ""

def listish(v):
    if v is None:return []
    if isinstance(v,(list,tuple)):return list(v)
    return [v]

def owned_site(websites):
    for u in listish(websites):
        h=host(u)
        if h and not any(x in h for x in PLATFORMS): return txt(u)
    return ""

def first(v):
    vals=listish(v)
    return txt(vals[0]) if vals else ""

def brand_name(v):
    if isinstance(v,dict):
        names=v.get("names")
        if isinstance(names,dict):return txt(names.get("primary"))
        return txt(v.get("name"))
    return ""

def address_text(v):
    # Overture addresses is usually a list of structs. Keep this defensive so schema drift does not kill the run.
    vals=listish(v)
    if not vals:return ""
    a=vals[0]
    if isinstance(a,dict):
        parts=[]
        for k in ("freeform","address_line","house_number","street","locality","postcode","region","country"):
            val=a.get(k)
            if val and txt(val) not in parts:parts.append(txt(val))
        return ", ".join(parts)
    return txt(a)

def commercial_category(cat,basic,name):
    hay=norm(" ".join([cat,basic,name]))
    if any(x in hay for x in BANNED_CATEGORY_KWS):return False
    return any(x in hay for x in POSITIVE_KWS)

def main():
    out=Path("results/gws_brussels_overture_first");out.mkdir(parents=True,exist_ok=True)
    con=duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2';")
    q=f"""
    SELECT id,names.primary AS name,basic_category,categories.primary AS category_primary,
           websites,socials,emails,phones,brand,addresses,confidence,operating_status,
           (bbox.xmin+bbox.xmax)/2.0 AS longitude,(bbox.ymin+bbox.ymax)/2.0 AS latitude
    FROM read_parquet('{OVERTURE}', hive_partitioning=1)
    WHERE bbox.xmax >= {MINLON} AND bbox.xmin <= {MAXLON}
      AND bbox.ymax >= {MINLAT} AND bbox.ymin <= {MAXLAT}
      AND (operating_status IS NULL OR operating_status='open')
      AND names.primary IS NOT NULL
    """
    cur=con.execute(q); cols=[d[0] for d in cur.description]
    places=[dict(zip(cols,x)) for x in cur.fetchall()]
    rows=[]
    cats=Counter()
    for p in places:
        name=txt(p.get("name")); cat=txt(p.get("category_primary")); basic=txt(p.get("basic_category"))
        cats[cat or basic or "(none)"]+=1
        phone=first(p.get("phones")); site=owned_site(p.get("websites")); brand=brand_name(p.get("brand"))
        chain=bool(any(x in norm(brand or name) for x in CHAIN_WORDS))
        commercial=commercial_category(cat,basic,name)
        conf=float(p.get("confidence") or 0)
        if not commercial or chain or not phone or site or conf<0.55:continue
        rows.append({
          "overture_id":txt(p.get("id")),"business_name":name,"category":cat,"basic_category":basic,
          "address":address_text(p.get("addresses")),"phone":phone,"email":first(p.get("emails")),
          "socials":json.dumps(p.get("socials"),ensure_ascii=False,default=str) if p.get("socials") else "",
          "websites_raw":json.dumps(p.get("websites"),ensure_ascii=False,default=str) if p.get("websites") else "",
          "confidence":conf,"latitude":p.get("latitude"),"longitude":p.get("longitude"),"brand":brand,
          "preliminary":"CURRENT_PLACE_NO_OWNED_SITE_PHONE",
        })
    # Prefer strong confidence + public email + social footprint. Diversity is maintained naturally by the broad category lane.
    rows.sort(key=lambda r:(float(r["confidence"]),bool(r["email"]),bool(r["socials"])),reverse=True)
    serious=rows[:2000]
    for i,r in enumerate(serious,1):r["serious_rank"]=i
    fields=list(serious[0].keys()) if serious else []
    with (out/"serious2000.csv").open("w",encoding="utf-8",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(serious)
    with (out/"all_current_no_owned_site_phone.csv").open("w",encoding="utf-8",newline="") as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0].keys()) if rows else []);w.writeheader();w.writerows(rows)
    with (out/"top_categories.csv").open("w",encoding="utf-8",newline="") as f:
        w=csv.writer(f);w.writerow(["category","count"]);w.writerows(cats.most_common(500))
    summary={
      "overture_release":RELEASE,"places_in_brussels_bbox":len(places),
      "current_commercial_no_owned_site_with_phone":len(rows),"serious_materialized":len(serious),
      "with_email_in_serious":sum(bool(r["email"]) for r in serious),
      "with_socials_in_serious":sum(bool(r["socials"]) for r in serious),
      "bbox":[MINLON,MINLAT,MAXLON,MAXLAT],
    }
    (out/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
    print("GWS_OVERTURE_FIRST_SUMMARY="+json.dumps(summary,ensure_ascii=False))
if __name__=="__main__":main()
