#!/usr/bin/env python3
from __future__ import annotations
import argparse,csv,json,re,time,random,unicodedata
from concurrent.futures import ThreadPoolExecutor,as_completed
from pathlib import Path
from urllib.parse import urlparse,parse_qs,unquote
import requests
from bs4 import BeautifulSoup

BLOCKED=(
 "facebook.com","instagram.com","tiktok.com","linkedin.com","youtube.com","x.com","twitter.com",
 "treatwell.","mytreatwell.","planity.com","fresha.com","salonkee.",
 "pagesdor.","goudengids.","bizique.","cylex-","opendi.","bolid.","garagebelgique.",
 "selfcity.","heures.be","openingsuren.","tripadvisor.","yelp.","google.","waze.",
 "ubereats.","deliveroo.","takeaway.","booking.com","pappers.","companyweb.","bottin.",
 "nosavis.","localguide.","beautynailhairsalons.","facebook.","pinterest.",
)
UA="Mozilla/5.0 (compatible; GWS-Brussels-WebGate/1.0; +public-business-verification)"

def norm(s):
    s=unicodedata.normalize("NFKD",str(s or "")).encode("ascii","ignore").decode().lower()
    s=re.sub(r"[^a-z0-9]+"," ",s)
    return re.sub(r"\s+"," ",s).strip()
def toks(s): return {x for x in norm(s).split() if len(x)>=3 and x not in {"the","les","des","rue","avenue","boulevard","chaussee","brussels","bruxelles","belgium","belgique"}}
def digits(s): return re.sub(r"\D","",str(s or ""))
def host(u):
    try:
        h=(urlparse(u).hostname or "").lower().strip(".")
        return h[4:] if h.startswith("www.") else h
    except:return ""
def blocked(h): return (not h) or any(x in h for x in BLOCKED)
def domain_name_score(h,name):
    stem=re.sub(r"[^a-z0-9]","",h.split(".")[0].lower())
    nts=[x for x in toks(name) if len(x)>=4]
    if not stem or not nts:return 0
    return max((1 if t in stem else 0) for t in nts)

def bing(q,session):
    r=session.get("https://www.bing.com/search",params={"q":q,"count":10},headers={"User-Agent":UA},timeout=20)
    if r.status_code in (429,403): raise RuntimeError("BING_BLOCKED_"+str(r.status_code))
    r.raise_for_status()
    s=BeautifulSoup(r.text,"html.parser"); out=[]
    for a in s.select("li.b_algo h2 a"):
        u=a.get("href",""); t=a.get_text(" ",strip=True)
        if u.startswith("http"): out.append((u,t))
    return out[:10]

def ddg(q,session):
    r=session.get("https://html.duckduckgo.com/html/",params={"q":q},headers={"User-Agent":UA},timeout=20)
    if r.status_code in (429,403): raise RuntimeError("DDG_BLOCKED_"+str(r.status_code))
    r.raise_for_status()
    s=BeautifulSoup(r.text,"html.parser"); out=[]
    for a in s.select("a.result__a"):
        u=a.get("href",""); t=a.get_text(" ",strip=True)
        if "uddg=" in u:
            try:u=unquote(parse_qs(urlparse(u).query).get("uddg",[""])[0])
            except:pass
        if u.startswith("http"): out.append((u,t))
    return out[:10]

def corroborate_owned(url,row,session):
    try:
        r=session.get(url,headers={"User-Agent":UA},timeout=15,allow_redirects=True)
        if r.status_code>=400:return False,"HTTP_"+str(r.status_code),r.url
        soup=BeautifulSoup(r.text[:800000],"html.parser")
        text=norm(soup.get_text(" ",strip=True))
        nt=toks(row["hub_name"])
        name_hits=sum(1 for t in nt if t in text)
        p=digits(row.get("overture_phone",""))
        phone_hit=bool(p and p[-8:] in digits(text))
        at=toks(row.get("hub_address",""))
        addr_hits=sum(1 for t in at if t in text)
        title=norm(soup.title.get_text(" ",strip=True) if soup.title else "")
        title_hits=sum(1 for t in nt if t in title)
        ok=phone_hit or (name_hits>=max(1,min(2,len(nt))) and addr_hits>=1) or (title_hits>=1 and addr_hits>=2)
        return ok,f"name_hits={name_hits};addr_hits={addr_hits};phone={int(phone_hit)};title_hits={title_hits}",r.url
    except Exception as e:return False,"FETCH_ERR:"+type(e).__name__,url

def one(row):
    s=requests.Session()
    name=row["hub_name"]; addr=row["hub_address"]; phone=row.get("overture_phone","")
    q1=f'"{name}" "{addr}"'
    q2=f'"{name}" "{phone}" Brussels' if phone else f'"{name}" Brussels "{row.get("hub_postalcode","")}"'
    engines=[]; links=[]; errors=[]
    for eng,fn,q in [("bing",bing,q1),("ddg",ddg,q1),("bing2",bing,q2)]:
        try:
            res=fn(q,s);engines.append(eng);links.extend((eng,u,t) for u,t in res)
        except Exception as e:errors.append(f"{eng}:{e}")
        time.sleep(0.15+random.random()*0.25)
    uniq=[];seen=set()
    for eng,u,t in links:
        h=host(u)
        if (u,h) in seen:continue
        seen.add((u,h));uniq.append((eng,u,t,h))
    owned=[]; corroborating=[]
    for eng,u,t,h in uniq:
        if blocked(h):
            corroborating.append(u);continue
        if domain_name_score(h,name)>=1:
            ok,why,final=corroborate_owned(u,row,s)
            if ok:owned.append((final,why))
    if owned:
        outcome="REJECT";reason="OWNED_SITE_FOUND"
    elif len(engines)>=2 and len(set(host(u) for u in corroborating if host(u)))>=2 and phone:
        outcome="HIGH";reason="VERIFIED_NO_WEBSITE"
    elif len(engines)>=2:
        outcome="MEDIUM";reason="NO_OWNED_SITE_FOUND_BUT_CORROBORATION_BELOW_HIGH_GATE"
    elif errors:
        outcome="ERROR_RETRYABLE";reason="SEARCH_ENGINE_PARTIAL_BLOCKER"
    else:
        outcome="UNCERTAIN";reason="INSUFFICIENT_EVIDENCE"
    out=dict(row)
    out.update({
      "outcome":outcome,"reason_code":reason,
      "search_engines_ok":"|".join(engines),"search_errors":"|".join(errors),
      "owned_site_found":owned[0][0] if owned else "",
      "owned_site_evidence":owned[0][1] if owned else "",
      "corroborating_urls":" | ".join(corroborating[:8]),
      "search_urls":" | ".join(u for _,u,_,_ in uniq[:15]),
    })
    return out

def main():
    ap=argparse.ArgumentParser();ap.add_argument("--input",required=True);ap.add_argument("--outdir",required=True);ap.add_argument("--workers",type=int,default=4)
    a=ap.parse_args();out=Path(a.outdir);out.mkdir(parents=True,exist_ok=True)
    rows=list(csv.DictReader(open(a.input,encoding="utf-8")))
    done=[]
    with ThreadPoolExecutor(max_workers=max(1,min(a.workers,6))) as ex:
        futs={ex.submit(one,r):r for r in rows}
        for i,f in enumerate(as_completed(futs),1):
            try:done.append(f.result())
            except Exception as e:
                r=dict(futs[f]);r.update({"outcome":"ERROR_HARD","reason_code":"WORKER_EXCEPTION","search_errors":repr(e)});done.append(r)
            if i%25==0:print(f"webgate {i}/{len(rows)}",flush=True)
    rank=lambda r:int(r.get("web_gate_rank") or 999999)
    done.sort(key=rank)
    fields=[]
    for r in done:
        for k in r:
            if k not in fields:fields.append(k)
    with (out/"web_gate_results.csv").open("w",encoding="utf-8",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields,extrasaction="ignore");w.writeheader();w.writerows(done)
    counts={}
    for r in done:counts[r["outcome"]]=counts.get(r["outcome"],0)+1
    summary={"attempts":len(done),"outcomes":counts,"high":counts.get("HIGH",0),"reject":counts.get("REJECT",0),"medium":counts.get("MEDIUM",0),"uncertain":counts.get("UNCERTAIN",0),"error_retryable":counts.get("ERROR_RETRYABLE",0),"error_hard":counts.get("ERROR_HARD",0)}
    (out/"web_gate_summary.json").write_text(json.dumps(summary,indent=2),encoding="utf-8")
    print("GWS_WEB_GATE_SUMMARY="+json.dumps(summary))
if __name__=="__main__":main()
