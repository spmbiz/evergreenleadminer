#!/usr/bin/env python3
"""High-precision luxury/high-end gate for bulk property leads.

Raw discovery stays broad. This file decides what is allowed to progress toward
canonical outreach data. Availability of an email/site is NOT premium evidence.
Only public first-party page evidence is used; nothing is inferred.
"""
from __future__ import annotations
import argparse,csv,json,re,time
from concurrent.futures import ThreadPoolExecutor,as_completed
from pathlib import Path
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup

HEADERS={"User-Agent":"EvergreenLeadMiner-PremiumGate/3.0 (+public-business-research)","Accept":"text/html,application/xhtml+xml","Accept-Language":"en-US,en;q=0.8"}
PREMIUM_BRANDS=("four seasons","ritz-carlton","ritz carlton","st. regis","st regis","waldorf astoria","mandarin oriental","rosewood","auberge","belmond","aman ","amanresorts","one&only","one and only","montage","pendry","viceroy","raffles","peninsula","park hyatt","1 hotel","proper hotel","nobu hotel","luxury collection","edition hotel","the edition")
HARD_REJECT=("motel 6","super 8","travelodge","days inn","econo lodge","econolodge","rodeway inn","quality inn","comfort inn","country inn","holiday inn express","hampton inn","fairfield inn","best western","la quinta","red roof","extended stay america","howard johnson","americas best value inn","surestay","microtel","budget inn","budget lodge")
NON_PROPERTY=("botanical garden","museum","university","hospital","medical center","campground","rv park","student housing","senior living","assisted living")
IDENTITY_PREMIUM=("luxury","luxurious","high-end","high end","upscale","five-star","five star","5-star","5 star","boutique hotel","boutique resort","private villa","luxury villa","luxury villas","exclusive villa","luxury residence","luxury residences","private residence","private residences","estate resort","luxury resort","luxury retreat","luxury lodge","design hotel","designer hotel","ultra-luxury","ultra luxury")
PORTFOLIO=("vacation rental management","vacation rentals","short-term rental","short term rental","property management","holiday home management","holiday homes","villa management","villa rentals","managed homes","managed properties","our properties","our villas","our homes","portfolio of homes","portfolio of properties","collection of villas","collection of homes","luxury rentals","serviced residences","serviced apartments")
VISUAL=("beachfront","oceanfront","waterfront","private pool","infinity pool","private beach","ski-in ski-out","ski in ski out","panoramic views","ocean view","sea view","mountain view","penthouse","architect-designed","architect designed","rooftop terrace")
FIELDS_EXTRA=["premium_v3_score","premium_v3_tier","premium_v3_selected","premium_v3_master_ready","premium_v3_reasons","premium_v3_source_url"]

def norm(x): return re.sub(r"\s+"," ",str(x or "")).strip()
def num(x):
    m=re.search(r"\d+(?:[.,]\d+)?",norm(x))
    try:return float(m.group(0).replace(",",".")) if m else None
    except:return None
def phrase(text,p):
    return re.search(r"(?<![a-z0-9])"+re.escape(p.lower()).replace(r"\ ",r"\s+")+r"(?![a-z0-9])",text.lower()) is not None
def hits(text,phrases): return [p for p in phrases if phrase(text,p)]

def fetch_identity(url,timeout=12):
    if not url:return "","",""
    tries=[url]+(["http://"+url[8:]] if url.startswith("https://") else [])
    for u in tries:
        try:
            r=requests.get(u,headers=HEADERS,timeout=timeout,allow_redirects=True)
            if r.status_code>=400:continue
            ct=(r.headers.get("content-type") or "").lower()
            if ct and "html" not in ct:continue
            s=BeautifulSoup(r.text[:2200000],"html.parser")
            title=norm(s.title.get_text(" ",strip=True) if s.title else "")
            metas=[]
            for attrs in ({"name":re.compile("^description$",re.I)},{"property":re.compile("^og:description$",re.I)},{"property":re.compile("^og:title$",re.I)}):
                n=s.find("meta",attrs=attrs)
                if n and n.get("content"):metas.append(norm(n.get("content")))
            for tag in s(["script","style","noscript","svg"]):tag.decompose()
            body=norm(s.get_text(" ",strip=True))[:180000]
            return r.url or u,norm(" ".join([title]+metas)),body
        except Exception:pass
    return "","",""

def evaluate(row,identity,body,src):
    typ=norm(row.get("hospitality_type")).lower()
    name=norm(row.get("name")); op=norm(row.get("operator")); brand=norm(row.get("brand"))
    strong_text=norm(" ".join([name,op,brand,identity])).lower()
    body_l=body.lower()
    full=strong_text+" "+body_l
    reasons=[]; score=0
    bad=hits(strong_text,HARD_REJECT)+hits(strong_text,NON_PROPERTY)
    if typ in {"motel","hostel"}:bad.append("type:"+typ)
    pbrands=hits(strong_text,PREMIUM_BRANDS)
    idprem=hits(strong_text,IDENTITY_PREMIUM)
    portfolio=hits(full,PORTFOLIO)
    visual=hits(full,VISUAL)
    stars=num(row.get("stars"))
    if pbrands: score+=55; reasons.append("premium-brand:"+",".join(pbrands[:2]))
    if stars is not None and stars>=5:score+=48;reasons.append("5-star")
    elif stars is not None and stars>=4:score+=32;reasons.append("4-star")
    if idprem:score+=min(50,25+6*(len(idprem)-1));reasons.append("identity-premium:"+",".join(idprem[:5]))
    if portfolio:score+=min(28,10+3*len(portfolio));reasons.append("portfolio:"+",".join(portfolio[:4]))
    if visual:score+=min(20,5+3*len(visual));reasons.append("visual:"+",".join(visual[:4]))
    if typ=="resort":score+=10
    elif typ=="chalet":score+=8
    elif typ=="hotel":score+=3
    rooms=num(row.get("rooms"))
    if rooms and rooms>=20 and (pbrands or idprem or (stars and stars>=4)):score+=6
    explicit_premium=bool(pbrands or idprem or (stars is not None and stars>=4))
    portfolio_premium=bool(portfolio and (pbrands or idprem))
    selected=False
    if not bad:
        if typ=="hotel": selected=explicit_premium and score>=35
        elif typ=="resort": selected=(explicit_premium and score>=38) or (len(visual)>=2 and score>=42)
        elif typ in {"apartment","guest_house","chalet","hospitality"}: selected=(portfolio_premium and score>=48) or (explicit_premium and len(visual)>=1 and score>=42)
        else: selected=explicit_premium and score>=45
    if bad:reasons.insert(0,"reject:"+",".join(bad[:3]))
    if not selected:tier="REJECT"
    elif score>=90:tier="S"
    elif score>=65:tier="A"
    else:tier="B"
    email=norm(row.get("public_email_osm")) or norm(row.get("public_email_web"))
    site=norm(row.get("website"))
    master_ready=selected and bool(email and site) and tier in {"S","A"}
    out=dict(row)
    out.update({"premium_v3_score":str(min(score,100)),"premium_v3_tier":tier,"premium_v3_selected":"YES" if selected else "NO","premium_v3_master_ready":"YES" if master_ready else "NO","premium_v3_reasons":" | ".join(reasons),"premium_v3_source_url":src})
    return out

def write(path,rows,fields):
    with path.open("w",encoding="utf-8",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields,extrasaction="ignore");w.writeheader();w.writerows(rows)

def main():
    ap=argparse.ArgumentParser();ap.add_argument("--input",required=True);ap.add_argument("--outdir",required=True);ap.add_argument("--workers",type=int,default=32);ap.add_argument("--max-web-checks",type=int,default=0);a=ap.parse_args()
    out=Path(a.outdir);out.mkdir(parents=True,exist_ok=True)
    with open(a.input,encoding="utf-8-sig",newline="") as f:rows=list(csv.DictReader(f))
    targets=[r for r in rows if norm(r.get("website"))]
    targets.sort(key=lambda r:int(float(norm(r.get("score")) or 0)),reverse=True)
    if a.max_web_checks>0:targets=targets[:a.max_web_checks]
    ev={};t0=time.time()
    with ThreadPoolExecutor(max_workers=max(1,a.workers)) as ex:
        fs={ex.submit(fetch_identity,r.get("website") or ""):id(r) for r in targets}
        for i,f in enumerate(as_completed(fs),1):
            try:ev[fs[f]]=f.result()
            except:ev[fs[f]]=("","","")
            if i%100==0:print(f"v3 checks {i}/{len(targets)}",flush=True)
    scored=[]
    for r in rows:
        src,ident,body=ev.get(id(r),("","",""));scored.append(evaluate(r,ident,body,src))
    scored.sort(key=lambda r:(r["premium_v3_master_ready"]!="YES",r["premium_v3_selected"]!="YES",-int(r["premium_v3_score"])))
    fields=list(dict.fromkeys(list(rows[0].keys() if rows else [])+FIELDS_EXTRA))
    sel=[r for r in scored if r["premium_v3_selected"]=="YES"]
    ready=[r for r in scored if r["premium_v3_master_ready"]=="YES"]
    rej=[r for r in scored if r["premium_v3_selected"]!="YES"]
    write(out/"premium_v3_all_scored.csv",scored,fields);write(out/"premium_v3_selected.csv",sel,fields);write(out/"premium_v3_master_ready.csv",ready,fields);write(out/"premium_v3_rejected.csv",rej,fields)
    summary={"input_candidates":len(rows),"websites_checked":len(targets),"premium_selected":len(sel),"master_ready":len(ready),"tiers":{t:sum(1 for r in sel if r["premium_v3_tier"]==t) for t in ("S","A","B")},"elapsed_seconds":round(time.time()-t0,2),"rule":"High-precision: premium evidence must appear in identity/title/meta, premium brand or >=4 stars; email/site alone never qualifies; budget/non-property rejected; MASTER-ready requires S/A + public email + official site."}
    (out/"premium_v3_summary.json").write_text(json.dumps(summary,indent=2),encoding="utf-8");print(json.dumps(summary,indent=2))
if __name__=="__main__":main()
