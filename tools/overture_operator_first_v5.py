#!/usr/bin/env python3
"""V5 operator-first premium hospitality harvester.

Discover premium hospitality operators/properties first, expand first-party
portfolios, then enrich public contacts. Overture Places is the discovery rail;
public first-party websites are the resolution/portfolio rail. Nothing is inferred.
"""
from __future__ import annotations
import argparse,csv,json,re,time,xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor,as_completed
from pathlib import Path
from typing import Dict,Iterable,List,Sequence,Tuple
from urllib.parse import urljoin,urlparse
import duckdb,requests
from bs4 import BeautifulSoup

UA="Mozilla/5.0 (compatible; PremiumHospitalityResearch/1.0; +public-business-data)"
PREMIUM=("luxury","luxurious","ultra-luxury","ultra luxury","high-end","high end","five-star","five star","5-star","5 star","boutique","design hotel","private villa","private villas","luxury villa","luxury villas","estate","private residence","private residences","serviced residence","serviced residences","resort","retreat","private island","beachfront","oceanfront","ski-in ski-out")
OPERATOR=("vacation rental","vacation rentals","vacation rental management","holiday rental","holiday rentals","holiday home","holiday homes","villa rental","villa rentals","villa management","property management","rental management","managed homes","managed properties","luxury rentals","luxury stays","luxury homes","luxury villas","serviced accommodation","serviced apartments","resort management")
HARD_REJECT=("hostel","backpacker","motel 6","super 8","travelodge","econo lodge","econolodge","rodeway inn","quality inn","comfort inn","days inn","red roof","budget inn","student housing","senior living","assisted living","campground","rv park","rv resort","timeshare sales","wedding planner")
BRANDS=("four seasons","ritz-carlton","ritz carlton","st. regis","st regis","waldorf astoria","mandarin oriental","rosewood","auberge resorts","belmond","aman","one&only","one and only","montage","pendry","viceroy","raffles","park hyatt","1 hotel","proper hotel","nobu hotel","luxury collection","edition","fairmont","six senses","banyan tree","capella","cheval blanc")
PORTFOLIO_HINTS=("properties","property","villas","villa","homes","residences","residence","rentals","stays","portfolio","collection","accommodation","accommodations","destinations","our resorts","our hotels","locations")
PROPERTY_PATH_HINTS=("/villa","/villas","/property","/properties","/home","/homes","/residence","/residences","/hotel","/hotels","/resort","/resorts","/stay","/stays","/destination","/destinations")
FIELDS=["source","overture_id","country","region","name","entity_type","category","brand","website","domain","public_email","public_phone","city","state","street","latitude","longitude","confidence","premium_score","operator_score","portfolio_signal","portfolio_count","portfolio_urls","contact_source_url","source_url","notes"]

def norm(x): return re.sub(r"\s+"," ",str(x or "")).strip()
def first(v): return norm(v[0]) if isinstance(v,(list,tuple)) and v else norm(v)
def domain(u):
    try:
        h=(urlparse(u).hostname or "").lower().strip("."); return h[4:] if h.startswith("www.") else h
    except:return ""
def root_domain(u):
    h=domain(u); p=h.split("."); return ".".join(p[-2:]) if len(p)>=2 else h
def brand_name(v):
    if not isinstance(v,dict): return ""
    n=v.get("names"); return norm(n.get("primary")) if isinstance(n,dict) else norm(v.get("name"))
def addr(v):
    if not isinstance(v,(list,tuple)) or not v or not isinstance(v[0],dict): return {"city":"","state":"","street":""}
    a=v[0]; lines=a.get("address_lines"); street=", ".join(norm(x) for x in lines if norm(x)) if isinstance(lines,(list,tuple)) else norm(lines or a.get("freeform"))
    return {"city":norm(a.get("locality") or a.get("city")),"state":norm(a.get("region") or a.get("state")),"street":street}
def hits(text,phrases):
    low=text.lower(); return [p for p in phrases if p in low]

def fetch_html(url,timeout=14):
    if not url:return "",""
    try:
        r=requests.get(url,headers={"User-Agent":UA},timeout=timeout,allow_redirects=True)
        if r.status_code>=400 or not r.text:return "",""
        ct=(r.headers.get("content-type") or "").lower()
        if not any(x in ct for x in ("html","xml","text")):return "",""
        return r.url,r.text
    except:return "",""

def emails(html):
    found=re.findall(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}",html or "",flags=re.I); out=[]; seen=set()
    for e in found:
        e=e.strip(".,;:()[]{}<>\"'"); low=e.lower()
        if low.endswith((".png",".jpg",".jpeg",".gif",".svg",".webp")) or "example." in low or "sentry" in low:continue
        if low not in seen:seen.add(low);out.append(e)
    return out

def homepage(url):
    final,html=fetch_html(url)
    if not html:return "","",[],[],""
    soup=BeautifulSoup(html,"html.parser"); title=norm(soup.title.get_text(" ",strip=True) if soup.title else ""); meta=[]
    for attrs in ({"name":re.compile("^description$",re.I)},{"property":re.compile("^og:description$",re.I)},{"property":re.compile("^og:title$",re.I)}):
        n=soup.find("meta",attrs=attrs)
        if n and n.get("content"):meta.append(norm(n.get("content")))
    links=[];seen=set();rd=root_domain(final or url)
    for a in soup.find_all("a",href=True):
        href=str(a.get("href") or "").strip()
        if not href or href.startswith(("#","mailto:","tel:","javascript:")):continue
        full=urljoin(final or url,href)
        if root_domain(full)!=rd:continue
        hay=(full+" "+norm(a.get_text(" ",strip=True))).lower()
        if any(h in hay for h in PORTFOLIO_HINTS):
            clean=urlparse(full)._replace(fragment="").geturl()
            if clean not in seen:seen.add(clean);links.append(clean)
        if len(links)>=12:break
    for tag in soup(["script","style","noscript","svg"]):tag.decompose()
    body=norm(soup.get_text(" ",strip=True))[:160000]
    return final or url,norm(" ".join([title]+meta)),links,emails(html),body

def score(r,identity,body,links):
    canonical=norm(" ".join([r.get("name",""),r.get("brand",""),r.get("category",""),r.get("domain","")])).lower(); page=norm(identity).lower(); full=canonical+" "+page+" "+body.lower()
    if hits(canonical,HARD_REJECT) or hits(page[:800],HARD_REJECT):return 0,0,"reject","NO"
    ph=hits(canonical+" "+page[:1500],PREMIUM); bh=hits(canonical,BRANDS); oh=hits(full[:80000],OPERATOR); vh=hits(full[:80000],("beachfront","oceanfront","private pool","infinity pool","private island","ski-in ski-out","penthouse"))
    p=o=0
    if bh:p+=48
    if ph:p+=min(38,18+4*len(ph))
    if vh:p+=min(18,4+3*len(vh))
    cat=(r.get("category") or "").lower(); p+=12 if "resort" in cat else (5 if "hotel" in cat else 0)
    if oh:o+=min(65,22+5*len(oh))
    if links:o+=min(25,5+2*len(links))
    if any(k in (r.get("name") or "").lower() for k in ("rentals","property management","vacation","villas","stays","homes")):o+=18
    return min(p,100),min(o,100),("operator" if o>=42 else "property"),("YES" if o>=42 or len(links)>=3 else "NO")

def property_links(seed,pages,max_pages=6,max_props=80):
    rd=root_domain(seed); candidates=[];seen=set()
    for sm in (urljoin(seed,"/sitemap.xml"),urljoin(seed,"/sitemap_index.xml")):
        _,xml=fetch_html(sm,10)
        if not xml:continue
        try:
            root=ET.fromstring(xml)
            for loc in root.findall(".//{*}loc"):
                u=norm(loc.text); path=urlparse(u).path.lower()
                if u and root_domain(u)==rd and any(h in path for h in PROPERTY_PATH_HINTS) and u not in seen:
                    seen.add(u);candidates.append(("",u))
                    if len(candidates)>=max_props:break
        except:pass
        if len(candidates)>=max_props:break
    for page in list(pages)[:max_pages]:
        final,html=fetch_html(page)
        if not html:continue
        soup=BeautifulSoup(html,"html.parser")
        for a in soup.find_all("a",href=True):
            href=str(a.get("href") or "").strip()
            if not href or href.startswith(("#","mailto:","tel:","javascript:")):continue
            u=urljoin(final or page,href); path=urlparse(u).path.lower(); text=norm(a.get_text(" ",strip=True))
            if root_domain(u)!=rd or not (any(h in path for h in PROPERTY_PATH_HINTS) or any(k in (path+" "+text.lower()) for k in ("villa","residence","resort","hotel","chalet","penthouse","estate"))):continue
            u=urlparse(u)._replace(fragment="",query="").geturl()
            if u in seen:continue
            seen.add(u);candidates.append((text if 3<=len(text)<=120 else "",u))
            if len(candidates)>=max_props:break
        if len(candidates)>=max_props:break
    out=[]
    for name,u in candidates[:max_props]:
        if not name:
            _,html=fetch_html(u,9)
            if html:
                soup=BeautifulSoup(html,"html.parser");name=norm(soup.title.get_text(" ",strip=True) if soup.title else "");name=re.split(r"\s+[|–—-]\s+",name)[0].strip()[:120]
        if name and not hits(name,HARD_REJECT):out.append((name,u))
    return out

def query(bbox,release,max_rows):
    minlon,minlat,maxlon,maxlat=map(float,bbox.split(","));con=duckdb.connect();con.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2';");path=f"s3://overturemaps-us-west-2/release/{release}/theme=places/type=place/*"
    q=f"""SELECT id,names.primary AS name,basic_category,taxonomy.primary AS taxonomy_primary,categories.primary AS category_primary,websites,emails,phones,brand,addresses,confidence,operating_status,(bbox.xmin+bbox.xmax)/2.0 AS longitude,(bbox.ymin+bbox.ymax)/2.0 AS latitude FROM read_parquet('{path}',hive_partitioning=1) WHERE bbox.xmin>={minlon} AND bbox.xmax<={maxlon} AND bbox.ymin>={minlat} AND bbox.ymax<={maxlat} AND (operating_status IS NULL OR operating_status='open') AND (COALESCE(list_contains(taxonomy.hierarchy,'lodging'),false) OR COALESCE(basic_category,'') IN ('hotel','resort','lodging','vacation_rental','property_management','real_estate_agency') OR COALESCE(categories.primary,'') ILIKE '%hotel%' OR COALESCE(categories.primary,'') ILIKE '%resort%' OR COALESCE(categories.primary,'') ILIKE '%vacation%rental%' OR COALESCE(categories.primary,'') ILIKE '%property%management%' OR COALESCE(categories.primary,'') ILIKE '%villa%' OR COALESCE(names.primary,'') ILIKE '%luxury%rental%' OR COALESCE(names.primary,'') ILIKE '%villa%rental%' OR COALESCE(names.primary,'') ILIKE '%vacation%rental%' OR COALESCE(names.primary,'') ILIKE '%property%management%') LIMIT {int(max_rows)}"""
    cur=con.execute(q);cols=[d[0] for d in cur.description];return [dict(zip(cols,x)) for x in cur.fetchall()]
def write(path,rows,fields):
    rows=list(rows)
    with path.open("w",encoding="utf-8",newline="") as f:w=csv.DictWriter(f,fieldnames=fields,extrasaction="ignore");w.writeheader();w.writerows(rows)

def main():
    ap=argparse.ArgumentParser();ap.add_argument("--bbox",required=True);ap.add_argument("--country",default="");ap.add_argument("--region",default="");ap.add_argument("--outdir",required=True);ap.add_argument("--release",default="2026-06-17.0");ap.add_argument("--max-rows",type=int,default=100000);ap.add_argument("--max-web-checks",type=int,default=5000);ap.add_argument("--workers",type=int,default=32);a=ap.parse_args();t0=time.time();out=Path(a.outdir);out.mkdir(parents=True,exist_ok=True)
    raw=query(a.bbox,a.release,a.max_rows);rows=[];seen=set()
    for x in raw:
        name=norm(x.get("name"));site=first(x.get("websites"));dom=domain(site);oid=norm(x.get("id"));ad=addr(x.get("addresses"));cat=norm(x.get("taxonomy_primary") or x.get("category_primary") or x.get("basic_category"));key=dom or (name.lower()+"|"+str(round(float(x.get("longitude") or 0),4))+"|"+str(round(float(x.get("latitude") or 0),4)))
        if not name or key in seen:continue
        seen.add(key);rows.append({"source":"Overture Places + first-party portfolio","overture_id":oid,"country":a.country,"region":a.region,"name":name,"entity_type":"unknown","category":cat,"brand":brand_name(x.get("brand")),"website":site,"domain":dom,"public_email":first(x.get("emails")),"public_phone":first(x.get("phones")),"city":ad["city"],"state":ad["state"],"street":ad["street"],"latitude":norm(x.get("latitude")),"longitude":norm(x.get("longitude")),"confidence":norm(x.get("confidence")),"premium_score":"0","operator_score":"0","portfolio_signal":"NO","portfolio_count":"0","portfolio_urls":"","contact_source_url":"Overture Places" if first(x.get("emails")) else "","source_url":f"https://explore.overturemaps.org/#id={oid}" if oid else "","notes":""})
    pool=[]
    for r in rows:
        ident=norm(" ".join([r["name"],r["brand"],r["category"]])).lower()
        if r["website"] and not hits(ident,HARD_REJECT) and (hits(ident,PREMIUM) or hits(ident,OPERATOR) or hits(ident,BRANDS) or any(k in r["category"].lower() for k in ("resort","vacation","property"))):pool.append(r)
    pool=pool[:a.max_web_checks];evidence={}
    with ThreadPoolExecutor(max_workers=max(1,a.workers)) as ex:
        fs={ex.submit(homepage,r["website"]):r for r in pool}
        for fut in as_completed(fs):
            r=fs[fut]
            try:evidence[id(r)]=fut.result()
            except:evidence[id(r)]=(r["website"],"",[],[],"")
    selected=[];operators=[];properties=[]
    for r in pool:
        final,ident,links,ems,body=evidence.get(id(r),(r["website"],"",[],[],""));r["website"]=final or r["website"];r["domain"]=domain(r["website"])
        if not r["public_email"] and ems:r["public_email"]=ems[0];r["contact_source_url"]=r["website"]
        ps,os,etype,port=score(r,ident,body,links);r["premium_score"]=str(ps);r["operator_score"]=str(os);r["entity_type"]=etype;r["portfolio_signal"]=port;r["portfolio_urls"]=" | ".join(links)
        keep=(etype=="operator" and os>=42 and (ps>=22 or hits((r["name"]+" "+ident).lower(),OPERATOR))) or (etype=="property" and ps>=42)
        if keep:selected.append(r);operators.append(r) if etype=="operator" else properties.append(r)
    def enrich(r):
        if r["public_email"]:return r["public_email"],r["contact_source_url"]
        final,html=fetch_html(r["website"]);em=emails(html)
        if em:return em[0],final
        if not html:return "",""
        soup=BeautifulSoup(html,"html.parser")
        for a_tag in soup.find_all("a",href=True):
            href=str(a_tag.get("href") or "");hay=(href+" "+norm(a_tag.get_text(" ",strip=True))).lower()
            if not any(k in hay for k in ("contact","about","reserv","booking","inquir")):continue
            u=urljoin(final or r["website"],href)
            if root_domain(u)!=root_domain(final or r["website"]):continue
            fu,txt=fetch_html(u);em=emails(txt)
            if em:return em[0],fu or u
        return "",""
    missing=[r for r in selected if not r["public_email"]]
    with ThreadPoolExecutor(max_workers=max(1,a.workers)) as ex:
        fs={ex.submit(enrich,r):r for r in missing}
        for fut in as_completed(fs):
            r=fs[fut]
            try:
                em,src=fut.result()
                if em:r["public_email"]=em;r["contact_source_url"]=src
            except:pass
    expansion=[]
    for op in sorted(operators,key=lambda r:(int(r["operator_score"]),int(r["premium_score"])),reverse=True)[:250]:
        purls=[u.strip() for u in op["portfolio_urls"].split("|") if u.strip()];props=property_links(op["website"],purls);op["portfolio_count"]=str(len(props))
        for pn,pu in props:expansion.append({"operator_name":op["name"],"operator_domain":op["domain"],"operator_email":op["public_email"],"operator_phone":op["public_phone"],"country":op["country"],"region":op["region"],"property_name":pn,"property_url":pu,"operator_website":op["website"]})
    selected.sort(key=lambda r:(int(r["operator_score"]),int(r["premium_score"]),bool(r["public_email"])),reverse=True);ready=[r for r in selected if r["public_email"] and (int(r["operator_score"])>=42 or int(r["premium_score"])>=55)]
    write(out/"v5_selected_accounts.csv",selected,FIELDS);write(out/"v5_operators.csv",operators,FIELDS);write(out/"v5_properties.csv",properties,FIELDS);write(out/"v5_contactable.csv",ready,FIELDS);write(out/"v5_portfolio_expansion.csv",expansion,["operator_name","operator_domain","operator_email","operator_phone","country","region","property_name","property_url","operator_website"])
    summary={"release":a.release,"country":a.country,"region":a.region,"bbox":a.bbox,"raw_query_rows":len(raw),"unique_candidates":len(rows),"web_prefilter":len(pool),"selected_accounts":len(selected),"operators":len(operators),"properties":len(properties),"contactable_selected":len(ready),"portfolio_properties_expanded":len(expansion),"operators_with_portfolio":sum(int(r.get("portfolio_count") or 0)>0 for r in operators),"emails_on_selected":sum(bool(r["public_email"]) for r in selected),"sites_on_selected":sum(bool(r["website"]) for r in selected),"elapsed_seconds":round(time.time()-t0,2)};(out/"v5_summary.json").write_text(json.dumps(summary,indent=2),encoding="utf-8");print(json.dumps(summary,indent=2))
if __name__=="__main__":main()
