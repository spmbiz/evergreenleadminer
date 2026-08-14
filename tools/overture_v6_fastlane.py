#!/usr/bin/env python3
"""V6 fast-lane hospitality/account harvester.

Zero per-lead HTTP: query public Overture Places in bulk and retain only records
with a website + public email already present. Apply cheap account-fit and contact
sanity gates so output is suitable for canonical MASTER dedupe / final QA.
Nothing is inferred.
"""
from __future__ import annotations
import argparse,csv,json,re,time
from pathlib import Path
from urllib.parse import urlparse
import duckdb

FIELDS=[
    "source","overture_id","country","region","name","category","brand",
    "website","domain","public_email","email_domain","email_domain_match",
    "public_phone","city","state","street","confidence","operator_score",
    "premium_score","fit_tier","source_url","notes"
]
OPERATOR=(
    "vacation rental","vacation rentals","vacation home","vacation homes",
    "holiday rental","holiday rentals","holiday home","holiday homes",
    "villa rental","villa rentals","villa management","property management",
    "rental management","short term rental","short-term rental","serviced apartment",
    "serviced apartments","serviced accommodation","luxury rentals","luxury stays",
    "managed homes","managed properties","condo rentals","cabin rentals",
    "chalet rentals","beach rentals","vacation property"
)
PREMIUM=(
    "luxury","boutique","villa","villas","resort","retreat","estate",
    "residence","residences","beachfront","oceanfront","waterfront","ski",
    "chalet","penthouse","private island","design hotel","collection"
)
HARD_REJECT=(
    "hostel","backpacker","motel 6","super 8","econo lodge","econolodge",
    "rodeway inn","quality inn","comfort inn","days inn","red roof","budget inn",
    "student housing","senior living","assisted living","campground","rv park",
    "rv resort","timeshare sales","wedding planner"
)
BAD_EMAIL_DOMAINS=(
    "example.com","example.org","example.net","sentry.io","cloudflare.com",
    "wixpress.com","squarespace.com","wordpress.com","mailchimp.com","hubspot.com",
    "tambourine.com","travelclick.com","booking.com","expedia.com","tripadvisor.com",
    "airbnb.com"
)
FREE_EMAIL=(
    "gmail.com","googlemail.com","outlook.com","hotmail.com","live.com","yahoo.com",
    "icloud.com","me.com","aol.com","proton.me","protonmail.com"
)
MULTIPART_SUFFIXES=("co.uk","com.au","com.br","com.mx","co.nz","co.za","com.pt","com.es","com.tr")

def norm(x): return re.sub(r"\s+"," ",str(x or "")).strip()
def first(v): return norm(v[0]) if isinstance(v,(list,tuple)) and v else norm(v)
def host(u):
    try:
        h=(urlparse(u).hostname or "").lower().strip(".")
        return h[4:] if h.startswith("www.") else h
    except:return ""
def root_host(h):
    h=(h or "").lower().strip(".")
    if not h:return ""
    for s in MULTIPART_SUFFIXES:
        if h==s:return h
        if h.endswith("."+s):
            parts=h.split("."); return ".".join(parts[-3:])
    parts=h.split("."); return ".".join(parts[-2:]) if len(parts)>=2 else h
def email_domain(e):
    e=norm(e).lower().strip("<>[](){}.,;:\"'")
    return e.rsplit("@",1)[1] if "@" in e else ""
def valid_email(e):
    e=norm(e).lower().strip("<>[](){}.,;:\"'")
    if not re.fullmatch(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}",e):return False
    d=email_domain(e)
    if d in BAD_EMAIL_DOMAINS or any(d.endswith("."+x) for x in BAD_EMAIL_DOMAINS):return False
    if any(x in e for x in ("example@","test@","noreply@","no-reply@","donotreply@")):return False
    return True
def brand_name(v):
    if not isinstance(v,dict):return ""
    n=v.get("names")
    return norm(n.get("primary")) if isinstance(n,dict) else norm(v.get("name"))
def addr(v):
    if not isinstance(v,(list,tuple)) or not v or not isinstance(v[0],dict):return {"city":"","state":"","street":""}
    a=v[0]; lines=a.get("address_lines")
    street=", ".join(norm(x) for x in lines if norm(x)) if isinstance(lines,(list,tuple)) else norm(lines or a.get("freeform"))
    return {"city":norm(a.get("locality") or a.get("city")),"state":norm(a.get("region") or a.get("state")),"street":street}
def hits(text,phrases):
    low=(text or "").lower(); return sum(1 for p in phrases if p in low)

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--bbox",required=True)
    ap.add_argument("--country",default=""); ap.add_argument("--region",default="")
    ap.add_argument("--outdir",required=True); ap.add_argument("--max-rows",type=int,default=250000)
    ap.add_argument("--release",default="2026-06-17.0"); a=ap.parse_args(); t0=time.time()
    minlon,minlat,maxlon,maxlat=map(float,a.bbox.split(",")); out=Path(a.outdir); out.mkdir(parents=True,exist_ok=True)
    con=duckdb.connect(); con.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2';")
    path=f"s3://overturemaps-us-west-2/release/{a.release}/theme=places/type=place/*"
    q=f"""
      SELECT id,names.primary AS name,basic_category,taxonomy.primary AS taxonomy_primary,
             categories.primary AS category_primary,websites,emails,phones,brand,addresses,
             confidence,operating_status
      FROM read_parquet('{path}',hive_partitioning=1)
      WHERE bbox.xmin>={minlon} AND bbox.xmax<={maxlon}
        AND bbox.ymin>={minlat} AND bbox.ymax<={maxlat}
        AND (operating_status IS NULL OR operating_status='open')
        AND websites IS NOT NULL AND len(websites)>0
        AND emails IS NOT NULL AND len(emails)>0
        AND (
          COALESCE(list_contains(taxonomy.hierarchy,'lodging'),false)
          OR COALESCE(basic_category,'') IN ('hotel','resort','lodging','vacation_rental','property_management','real_estate_agency')
          OR COALESCE(categories.primary,'') ILIKE '%hotel%'
          OR COALESCE(categories.primary,'') ILIKE '%resort%'
          OR COALESCE(categories.primary,'') ILIKE '%vacation%rental%'
          OR COALESCE(categories.primary,'') ILIKE '%property%management%'
          OR COALESCE(categories.primary,'') ILIKE '%villa%'
          OR COALESCE(names.primary,'') ILIKE '%vacation%rental%'
          OR COALESCE(names.primary,'') ILIKE '%property%management%'
          OR COALESCE(names.primary,'') ILIKE '%villa%rental%'
        )
      LIMIT {int(a.max_rows)}
    """
    cur=con.execute(q); cols=[d[0] for d in cur.description]; raw=[dict(zip(cols,r)) for r in cur.fetchall()]
    rows=[]; seen=set(); reject={"invalid_email":0,"domain_mismatch":0,"hard_reject":0,"weak_fit":0,"duplicate":0}
    for x in raw:
        name=norm(x.get("name")); site=first(x.get("websites")); email=first(x.get("emails")); phone=first(x.get("phones"))
        dom=host(site); ed=email_domain(email); cat=norm(x.get("taxonomy_primary") or x.get("category_primary") or x.get("basic_category")); brand=brand_name(x.get("brand")); ad=addr(x.get("addresses"))
        if not name or not dom or not valid_email(email): reject["invalid_email"]+=1; continue
        identity=norm(" ".join([name,brand,cat])).lower()
        if hits(identity,HARD_REJECT): reject["hard_reject"]+=1; continue
        same=(root_host(dom)==root_host(ed)); free=(root_host(ed) in FREE_EMAIL)
        if not (same or free): reject["domain_mismatch"]+=1; continue
        oh=hits(identity,OPERATOR); ph=hits(identity,PREMIUM)
        op_score=min(100,(48 if oh else 0)+(12*min(3,oh))+(15 if any(k in name.lower() for k in ("rentals","property management","vacation","villas","homes")) else 0))
        p_score=min(100,(20 if "resort" in cat.lower() else 0)+(10 if "hotel" in cat.lower() else 0)+12*min(4,ph))
        operatorish=(op_score>=48 or any(k in cat.lower() for k in ("vacation","property management")))
        propertyish=(p_score>=34)
        if not (operatorish or propertyish): reject["weak_fit"]+=1; continue
        key=root_host(dom) or (name.lower()+"|"+ad["city"].lower())
        if key in seen: reject["duplicate"]+=1; continue
        seen.add(key)
        tier="A" if operatorish and (op_score>=60 or p_score>=24) else ("A" if p_score>=48 else "B")
        oid=norm(x.get("id")); rows.append({
          "source":"Overture Places V6 fast-lane","overture_id":oid,"country":a.country,"region":a.region,
          "name":name,"category":cat,"brand":brand,"website":site,"domain":dom,"public_email":email,
          "email_domain":ed,"email_domain_match":"YES" if same else "FREE_WEBMAIL","public_phone":phone,
          "city":ad["city"],"state":ad["state"],"street":ad["street"],"confidence":norm(x.get("confidence")),
          "operator_score":str(op_score),"premium_score":str(p_score),"fit_tier":tier,
          "source_url":f"https://explore.overturemaps.org/#id={oid}" if oid else "",
          "notes":"Public Overture site+email; zero-HTTP V6 cheap-screen. Final MASTER dedupe/QA required."
        })
    rows.sort(key=lambda r:(r["fit_tier"]=="A",int(r["operator_score"]),int(r["premium_score"])),reverse=True)
    with (out/"v6_fast_ready.csv").open("w",encoding="utf-8",newline="") as f:
        w=csv.DictWriter(f,fieldnames=FIELDS); w.writeheader(); w.writerows(rows)
    summary={"release":a.release,"country":a.country,"region":a.region,"bbox":a.bbox,"raw_site_email_rows":len(raw),"fast_ready":len(rows),"tier_a":sum(r['fit_tier']=='A' for r in rows),"tier_b":sum(r['fit_tier']=='B' for r in rows),"rejects":reject,"elapsed_seconds":round(time.time()-t0,2)}
    (out/"v6_fast_summary.json").write_text(json.dumps(summary,indent=2),encoding="utf-8"); print(json.dumps(summary,indent=2))

if __name__=="__main__": main()
