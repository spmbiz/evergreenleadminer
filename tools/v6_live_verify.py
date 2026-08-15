#!/usr/bin/env python3
"""Fast current-web verifier for V6 hospitality candidates.

Verifies current first-party hospitality identity. Generic long-term property
management / real-estate services are not hospitality: they must show explicit
short-stay/vacation/holiday accommodation evidence on the current website.
Strong hospitality names may only bypass a missing textual brand match when a
first-party email/domain match independently supports the identity.
"""
from __future__ import annotations
import argparse,csv,html as htmlmod,re,time
from concurrent.futures import ThreadPoolExecutor,as_completed
from pathlib import Path
from urllib.parse import urljoin,urlparse
import requests

HOSP=("vacation rental","vacation rentals","vacation home","holiday rental","holiday rentals","holiday home","short-term rental","short term rental","short stay","serviced apartment","aparthotel","hotel","resort","villa","villas","cabin","cabins","chalet","lodging","accommodation","guest house","guesthouse","booking")
STR_PROOF=("vacation rental","vacation rentals","vacation home","vacation homes","holiday rental","holiday rentals","holiday home","holiday homes","short-term rental","short term rental","short stay","nightly rental","airbnb","vrbo","serviced apartment","serviced apartments","serviced accommodation","aparthotel","villa rental","villa rentals","cabin rental","cabin rentals","chalet rental","chalet rentals")
PARKED=("domain is for sale","this domain is for sale","buy this domain","domain may be for sale","expired domain","website is for sale","parked free","sedo domain parking","hugedomains","afternic","dan.com","coming soon")
CLOSED=("permanently closed","ceased operations","we have closed","no longer operating","business has closed","closed our doors")
UA="Mozilla/5.0 (compatible; AIProdLeadVerifier/1.3; public-business-research)"
BAD_IG_PREFIXES=("p/","reel/","reels/","stories/","explore/","accounts/","direct/","about/","legal/","developer/")

def norm(x): return re.sub(r"\s+"," ",str(x or "")).strip()
def host(u):
    try:
        h=(urlparse(u).hostname or "").lower().strip(".")
        return h[4:] if h.startswith("www.") else h
    except:return ""
def tokens(name):
    stop={"hotel","resort","vacation","rentals","rental","property","management","home","homes","villa","villas","the","and","of","at","in","llc","inc","company"}
    return [x for x in re.findall(r"[a-z0-9]+",name.lower()) if len(x)>=4 and x not in stop]
def extract_instagram(raw_html,base_url):
    for href in re.findall(r"href\s*=\s*[\"']([^\"']+)[\"']",raw_html or "",flags=re.I):
        try:
            u=urljoin(base_url,htmlmod.unescape(href).strip());p=urlparse(u);h=(p.hostname or "").lower().strip(".")
            if h.startswith("www."): h=h[4:]
            if h!="instagram.com": continue
            path=(p.path or "").strip("/")
            if not path or any(path.lower().startswith(x) for x in BAD_IG_PREFIXES): continue
            handle=path.split("/",1)[0].strip()
            if re.fullmatch(r"[A-Za-z0-9._]{1,30}",handle): return f"https://www.instagram.com/{handle}/"
        except Exception: continue
    return ""
def verify(row,timeout):
    url=norm(row.get("website"));name=norm(row.get("name"));email=norm(row.get("public_email"));cat=norm(row.get("category"))
    out=dict(row);out.update({"live_status":"UNCERTAIN","http_status":"","final_url":"","hospitality_hits":"0","identity_hits":"0","email_on_homepage":"NO","instagram":"","instagram_source_url":"","live_reason":""})
    try:
        r=requests.get(url,headers={"User-Agent":UA},timeout=timeout,allow_redirects=True);out["http_status"]=str(r.status_code);out["final_url"]=r.url
        if r.status_code>=400: out["live_reason"]=f"HTTP_{r.status_code}";return out
        ig=extract_instagram(r.text,r.url)
        if ig: out["instagram"]=ig;out["instagram_source_url"]=r.url
        text=re.sub(r"<[^>]+>"," ",r.text[:900000]).lower();text=re.sub(r"\s+"," ",text)
        if any(x in text for x in PARKED): out["live_status"]="REJECT";out["live_reason"]="PARKED_OR_FOR_SALE";return out
        if any(x in text for x in CLOSED): out["live_status"]="REJECT";out["live_reason"]="CLOSED_SIGNAL";return out
        identity=(name+" "+cat).lower()
        generic_pm=("property management" in identity or "property manager" in identity) and not any(x in identity for x in STR_PROOF)
        generic_real_estate=("real estate" in identity or "real_estate" in identity) and not any(x in identity for x in STR_PROOF)
        explicit_str=any(x in text for x in STR_PROOF)
        if generic_pm and not explicit_str:
            out["live_status"]="REJECT";out["live_reason"]="GENERIC_PROPERTY_MANAGEMENT_NOT_SHORT_STAY";return out
        if generic_real_estate and not explicit_str:
            out["live_status"]="REJECT";out["live_reason"]="GENERIC_REAL_ESTATE_NOT_SHORT_STAY";return out
        hh=sum(1 for x in HOSP if x in text);it=sum(1 for x in tokens(name) if x in text)
        out["hospitality_hits"]=str(hh);out["identity_hits"]=str(it);out["email_on_homepage"]="YES" if email and email.lower() in text else "NO"
        strong_name=any(x in identity for x in ("vacation rental","vacation rentals","holiday rental","short-term rental","short term rental","cabin rental","cabin rentals","chalet rental","villa rental","serviced apartment","aparthotel"))
        email_match=str(row.get("email_domain_match") or "").strip().lower() in {"yes","true","1","match","matched"}
        identity_ok=it>=1 or (strong_name and email_match)
        if hh>=2 and identity_ok: out["live_status"]="HIGH";out["live_reason"]="CURRENT_HOSPITALITY_IDENTITY"
        elif hh>=1 and identity_ok: out["live_status"]="MEDIUM";out["live_reason"]="CURRENT_WEAK_HOSPITALITY_IDENTITY"
        elif strong_name and it==0 and not email_match:
            out["live_status"]="REJECT";out["live_reason"]="STRONG_NAME_WITHOUT_FIRST_PARTY_IDENTITY_PROOF"
        else: out["live_status"]="UNCERTAIN";out["live_reason"]="INSUFFICIENT_CURRENT_IDENTITY_PROOF"
        return out
    except requests.RequestException as e: out["live_reason"]="NETWORK_"+type(e).__name__.upper();return out

def main():
    ap=argparse.ArgumentParser();ap.add_argument("--input",required=True);ap.add_argument("--outdir",required=True);ap.add_argument("--workers",type=int,default=64);ap.add_argument("--timeout",type=float,default=7.0)
    a=ap.parse_args();out=Path(a.outdir);out.mkdir(parents=True,exist_ok=True);t0=time.time()
    with open(a.input,encoding="utf-8",newline="") as f: rows=list(csv.DictReader(f))
    verified=[]
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for fut in as_completed([ex.submit(verify,r,a.timeout) for r in rows]): verified.append(fut.result())
    verified.sort(key=lambda r:(r.get("live_status")!="HIGH",r.get("live_status")!="MEDIUM",-(int(r.get("operator_score") or 0)),-(int(r.get("premium_score") or 0)),r.get("name","").lower()))
    fields=list(verified[0].keys()) if verified else []
    with (out/"v6_live_verified.csv").open("w",encoding="utf-8",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(verified)
    keep=[r for r in verified if r["live_status"] in ("HIGH","MEDIUM")]
    with (out/"v6_live_ready.csv").open("w",encoding="utf-8",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(keep)
    import json
    summary={"input_fast_ready":len(rows),"live_high":sum(r['live_status']=='HIGH' for r in verified),"live_medium":sum(r['live_status']=='MEDIUM' for r in verified),"live_reject":sum(r['live_status']=='REJECT' for r in verified),"live_uncertain":sum(r['live_status']=='UNCERTAIN' for r in verified),"live_ready":len(keep),"instagram_found":sum(bool(r.get('instagram')) for r in keep),"elapsed_seconds":round(time.time()-t0,2)}
    (out/"v6_live_summary.json").write_text(json.dumps(summary,indent=2),encoding="utf-8");print(json.dumps(summary,indent=2))
if __name__=="__main__":main()
