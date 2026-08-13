#!/usr/bin/env python3
"""Bulk Overture Places lodging harvester.

Queries the official Overture GeoParquet release remotely with DuckDB, bounded
by a geographic shard. It converts public place fields into the same lead shape
used by the OSM harvester. Premium qualification is deliberately performed by
premium_property_filter_v3.py afterwards.
"""
from __future__ import annotations
import argparse,csv,json,re,time
from pathlib import Path
from urllib.parse import urlparse
import duckdb

FIELDS=["source","osm_id","country","region","name","operator","brand","hospitality_type","city","state","postcode","street","housenumber","website","domain","public_email_osm","public_email_web","email_source_url","public_phone","rooms","beds","stars","score","priority","source_url","notes","overture_id","overture_category","overture_basic_category","overture_confidence","socials","longitude","latitude"]

def norm(x):
    if x is None:return ""
    return re.sub(r"\s+"," ",str(x)).strip()
def first(v):
    if v is None:return ""
    if isinstance(v,(list,tuple)):return norm(v[0]) if v else ""
    return norm(v)
def domain(u):
    try:
        h=(urlparse(u).hostname or "").lower().strip(".")
        return h[4:] if h.startswith("www.") else h
    except:return ""
def dictish(v):
    if isinstance(v,dict):return v
    return {}
def address_parts(v):
    if not isinstance(v,(list,tuple)) or not v:return {"city":"","state":"","postcode":"","street":"","housenumber":""}
    a=dictish(v[0]); free=norm(a.get("freeform"))
    # Overture address field availability varies by source; keep freeform in street when detailed fields are absent.
    return {"city":norm(a.get("locality") or a.get("city")),"state":norm(a.get("region") or a.get("state")),"postcode":norm(a.get("postcode")),"street":norm(a.get("address_lines") or free),"housenumber":""}
def brand_name(v):
    d=dictish(v)
    names=d.get("names")
    if isinstance(names,dict):return norm(names.get("primary"))
    return norm(d.get("name"))
def score(row):
    s=15
    if row["website"]:s+=25
    if row["public_email_osm"]:s+=25
    if row["public_phone"]:s+=8
    n=(row["name"]+" "+row["brand"]+" "+row["overture_category"]).lower()
    for k in ("luxury","villa","resort","boutique","estate","residence","chalet","beachfront","oceanfront"):
        if k in n:s+=5
    try:
        c=float(row["overture_confidence"] or 0)
        if c>=0.9:s+=8
        elif c>=0.7:s+=4
    except:pass
    return min(s,100)
def esc(s):return str(s).replace("'","''")

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--bbox",required=True,help="minlon,minlat,maxlon,maxlat")
    ap.add_argument("--country",default="");ap.add_argument("--region",default="")
    ap.add_argument("--outdir",required=True);ap.add_argument("--max-rows",type=int,default=150000)
    ap.add_argument("--release",default="2026-06-17.0")
    a=ap.parse_args(); minlon,minlat,maxlon,maxlat=map(float,a.bbox.split(","))
    out=Path(a.outdir);out.mkdir(parents=True,exist_ok=True);t0=time.time()
    con=duckdb.connect();con.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2';")
    path=f"s3://overturemaps-us-west-2/release/{a.release}/theme=places/type=place/*"
    # Lodging hierarchy is the broad recall gate; generic motel/hostel/budget rows are discarded later by v3.
    q=f"""
    SELECT id,names.primary AS name,basic_category,taxonomy.primary AS taxonomy_primary,
           categories.primary AS category_primary,websites,socials,emails,phones,brand,addresses,
           confidence,operating_status,
           (bbox.xmin+bbox.xmax)/2.0 AS longitude,(bbox.ymin+bbox.ymax)/2.0 AS latitude
    FROM read_parquet('{path}', hive_partitioning=1)
    WHERE bbox.xmin >= {minlon} AND bbox.xmax <= {maxlon}
      AND bbox.ymin >= {minlat} AND bbox.ymax <= {maxlat}
      AND (operating_status IS NULL OR operating_status='open')
      AND (
        COALESCE(list_contains(taxonomy.hierarchy,'lodging'),false)
        OR COALESCE(basic_category,'') IN ('hotel','resort','lodging','vacation_rental','bed_and_breakfast','hostel','motel')
        OR COALESCE(categories.primary,'') ILIKE '%hotel%'
        OR COALESCE(categories.primary,'') ILIKE '%resort%'
        OR COALESCE(categories.primary,'') ILIKE '%vacation_rental%'
      )
    LIMIT {int(a.max_rows)}
    """
    cur=con.execute(q); cols=[d[0] for d in cur.description]; raw=[dict(zip(cols,r)) for r in cur.fetchall()]
    rows=[];seen=set()
    for r in raw:
        name=norm(r.get("name"));
        if not name:continue
        site=first(r.get("websites")); dom=domain(site)
        email=first(r.get("emails")); phone=first(r.get("phones")); addr=address_parts(r.get("addresses"))
        oid=norm(r.get("id")); cat=norm(r.get("taxonomy_primary") or r.get("category_primary") or r.get("basic_category"))
        key=(dom or (name.lower()+"|"+str(round(float(r.get('longitude') or 0),4))+"|"+str(round(float(r.get('latitude') or 0),4))))
        if key in seen:continue
        seen.add(key)
        row={"source":"Overture Maps Places","osm_id":"","country":a.country,"region":a.region,"name":name,"operator":"","brand":brand_name(r.get("brand")),"hospitality_type":norm(r.get("basic_category") or cat),"city":addr["city"],"state":addr["state"],"postcode":addr["postcode"],"street":addr["street"],"housenumber":addr["housenumber"],"website":site,"domain":dom,"public_email_osm":email,"public_email_web":"","email_source_url":"Overture Places" if email else "","public_phone":phone,"rooms":"","beds":"","stars":"","score":"0","priority":"C","source_url":f"https://explore.overturemaps.org/#id={oid}" if oid else "","notes":"Public Overture Places record; premium evidence verified downstream.","overture_id":oid,"overture_category":cat,"overture_basic_category":norm(r.get("basic_category")),"overture_confidence":norm(r.get("confidence")),"socials":json.dumps(r.get("socials"),ensure_ascii=False,default=str) if r.get("socials") else "","longitude":norm(r.get("longitude")),"latitude":norm(r.get("latitude"))}
        row["score"]=str(score(row));row["priority"]="A" if int(row["score"])>=70 else ("B" if int(row["score"])>=45 else "C")
        rows.append(row)
    rows.sort(key=lambda x:int(x["score"]),reverse=True)
    with (out/"overture_lodging_all.csv").open("w",encoding="utf-8",newline="") as f:
        w=csv.DictWriter(f,fieldnames=FIELDS,extrasaction="ignore");w.writeheader();w.writerows(rows)
    summary={"release":a.release,"country":a.country,"region":a.region,"bbox":[minlon,minlat,maxlon,maxlat],"raw_query_rows":len(raw),"unique_lodging_candidates":len(rows),"with_website":sum(bool(x['website']) for x in rows),"with_public_email":sum(bool(x['public_email_osm']) for x in rows),"with_phone":sum(bool(x['public_phone']) for x in rows),"elapsed_seconds":round(time.time()-t0,2)}
    (out/"overture_summary.json").write_text(json.dumps(summary,indent=2),encoding="utf-8");print(json.dumps(summary,indent=2))
if __name__=="__main__":main()
