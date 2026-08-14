#!/usr/bin/env python3
"""Fast current-web verifier for V6 fast-lane CSV.

Checks only cheap-screen survivors, not the whole raw dataset. It verifies that
the published website is currently reachable and still looks like a hospitality/
short-stay business. No email inference. Rows with blocks/timeouts are withheld.
"""
from __future__ import annotations
import argparse,csv,re,time
from concurrent.futures import ThreadPoolExecutor,as_completed
from pathlib import Path
from urllib.parse import urlparse
import requests

HOSP=("vacation","rental","rentals","hotel","resort","villa","villas","cabin","cabins","chalet","lodging","accommodation","property management","holiday","guest","booking","stay")
PARKED=("domain is for sale","this domain is for sale","buy this domain","domain may be for sale","expired domain","website is for sale","parked free","sedo domain parking","hugedomains","afternic","dan.com","coming soon")
CLOSED=("permanently closed","ceased operations","we have closed","no longer operating","business has closed","closed our doors")
UA="Mozilla/5.0 (compatible; AIProdLeadVerifier/1.0; public-business-research)"

def norm(x): return re.sub(r"\s+"," ",str(x or "")).strip()
def host(u):
    try:
        h=(urlparse(u).hostname or "").lower().strip(".")
        return h[4:] if h.startswith("www.") else h
    except:return ""
def tokens(name):
    stop={"hotel","resort","vacation","rentals","rental","property","management","home","homes","villa","villas","the","and","of","at","in","llc","inc","company"}
    return [x for x in re.findall(r"[a-z0-9]+",name.lower()) if len(x)>=4 and x not in stop]
def verify(row,timeout):
    url=norm(row.get("website")); name=norm(row.get("name")); email=norm(row.get("public_email"))
    out=dict(row); out.update({"live_status":"UNCERTAIN","http_status":"","final_url":"","hospitality_hits":"0","identity_hits":"0","email_on_homepage":"NO","live_reason":""})
    try:
        r=requests.get(url,headers={"User-Agent":UA},timeout=timeout,allow_redirects=True)
        out["http_status"]=str(r.status_code); out["final_url"]=r.url
        if r.status_code>=400:
            out["live_reason"]=f"HTTP_{r.status_code}"; return out
        text=re.sub(r"<[^>]+>"," ",r.text[:900000]).lower()
        text=re.sub(r"\s+"," ",text)
        if any(x in text for x in PARKED): out["live_status"]="REJECT";out["live_reason"]="PARKED_OR_FOR_SALE";return out
        if any(x in text for x in CLOSED): out["live_status"]="REJECT";out["live_reason"]="CLOSED_SIGNAL";return out
        hh=sum(1 for x in HOSP if x in text); it=sum(1 for x in tokens(name) if x in text)
        out["hospitality_hits"]=str(hh); out["identity_hits"]=str(it); out["email_on_homepage"]="YES" if email and email.lower() in text else "NO"
        # Strong operator names may have zero identity tokens after stopword removal; hospitality proof is then sufficient.
        strong_name=any(x in name.lower() for x in ("vacation rental","vacation rentals","holiday rental","cabin rental","cabin rentals","chalet rental","villa rental","property management"))
        if hh>=2 and (it>=1 or strong_name): out["live_status"]="HIGH";out["live_reason"]="CURRENT_HOSPITALITY_IDENTITY"
        elif hh>=1 and (it>=1 or strong_name): out["live_status"]="MEDIUM";out["live_reason"]="CURRENT_WEAK_HOSPITALITY_IDENTITY"
        else: out["live_status"]="UNCERTAIN";out["live_reason"]="INSUFFICIENT_CURRENT_IDENTITY_PROOF"
        return out
    except requests.RequestException as e:
        out["live_reason"]="NETWORK_"+type(e).__name__.upper();return out


def main():
    ap=argparse.ArgumentParser();ap.add_argument("--input",required=True);ap.add_argument("--outdir",required=True);ap.add_argument("--workers",type=int,default=64);ap.add_argument("--timeout",type=float,default=7.0)
    a=ap.parse_args();out=Path(a.outdir);out.mkdir(parents=True,exist_ok=True);t0=time.time()
    with open(a.input,encoding="utf-8",newline="") as f: rows=list(csv.DictReader(f))
    verified=[]
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs=[ex.submit(verify,r,a.timeout) for r in rows]
        for fut in as_completed(futs): verified.append(fut.result())
    # deterministic priority/order from original fast scores
    verified.sort(key=lambda r:(r.get("live_status")!="HIGH",r.get("live_status")!="MEDIUM",-(int(r.get("operator_score") or 0)),-(int(r.get("premium_score") or 0)),r.get("name","").lower()))
    fields=list(verified[0].keys()) if verified else []
    with (out/"v6_live_verified.csv").open("w",encoding="utf-8",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(verified)
    keep=[r for r in verified if r["live_status"] in ("HIGH","MEDIUM")]
    with (out/"v6_live_ready.csv").open("w",encoding="utf-8",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(keep)
    import json
    summary={"input_fast_ready":len(rows),"live_high":sum(r['live_status']=='HIGH' for r in verified),"live_medium":sum(r['live_status']=='MEDIUM' for r in verified),"live_reject":sum(r['live_status']=='REJECT' for r in verified),"live_uncertain":sum(r['live_status']=='UNCERTAIN' for r in verified),"live_ready":len(keep),"elapsed_seconds":round(time.time()-t0,2)}
    (out/"v6_live_summary.json").write_text(json.dumps(summary,indent=2),encoding="utf-8");print(json.dumps(summary,indent=2))
if __name__=="__main__":main()
