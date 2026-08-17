#!/usr/bin/env python3
"""Strict current-web verifier + Commercial Fit V2 gate for Hospitality.

Discovery is allowed to be broad. Canonical admission is not. A reachable site is
never sufficient on its own: the current page must prove the target identity and
the deterministic commercial-fit gate must return A/B. C is retained for review;
X is rejected. No PERMISSIVE rows enter v6_live_ready.csv.
"""
from __future__ import annotations
import argparse,csv,html as htmlmod,json,re,time
from concurrent.futures import ThreadPoolExecutor,as_completed
from pathlib import Path
from urllib.parse import urljoin,urlparse
import requests
from hospitality_quality_v2 import assess_record,sanitize_social

HOSP=("vacation rental","vacation rentals","vacation home","holiday rental","holiday rentals","holiday home","short-term rental","short term rental","short stay","serviced apartment","aparthotel","hotel","resort","villa","villas","cabin","cabins","chalet","lodging","accommodation","guest house","guesthouse")
STR_PROOF=("vacation rental","vacation rentals","vacation home","vacation homes","holiday rental","holiday rentals","holiday home","holiday homes","short-term rental","short term rental","short stay","nightly rental","airbnb","vrbo","serviced apartment","serviced apartments","serviced accommodation","aparthotel","villa rental","villa rentals","cabin rental","cabin rentals","chalet rental","chalet rentals")
PARKED=("domain is for sale","this domain is for sale","buy this domain","domain may be for sale","expired domain","website is for sale","parked free","sedo domain parking","hugedomains","afternic","dan.com")
CLOSED=("permanently closed","ceased operations","we have closed","no longer operating","business has closed","closed our doors")
UA="Mozilla/5.0 (compatible; AIProdLeadVerifier/2.1; public-business-research)"
BAD_IG_PREFIXES=("p/","reel/","reels/","stories/","explore/","accounts/","direct/","about/","legal/","developer/","privacy/","terms/","help/")

def norm(x):return re.sub(r"\s+"," ",str(x or "")).strip()
def tokens(name):
 stop={"hotel","resort","vacation","rentals","rental","property","management","home","homes","villa","villas","the","and","of","at","in","llc","inc","company"}
 return [x for x in re.findall(r"[a-z0-9]+",name.lower()) if len(x)>=4 and x not in stop]
def extract_instagram(raw_html,base_url):
 for href in re.findall(r"href\s*=\s*[\"']([^\"']+)[\"']",raw_html or "",flags=re.I):
  try:
   u=urljoin(base_url,htmlmod.unescape(href).strip());p=urlparse(u);h=(p.hostname or "").lower().strip(".")
   if h.startswith("www."):h=h[4:]
   if h!="instagram.com":continue
   path=(p.path or "").strip("/")
   if not path or any(path.lower().startswith(x) for x in BAD_IG_PREFIXES):continue
   clean=sanitize_social(u,"instagram")
   if clean:return clean
  except Exception:continue
 return ""
def page_text(raw_html):
 text=re.sub(r"<script\b[^>]*>.*?</script>"," ",raw_html or "",flags=re.I|re.S)
 text=re.sub(r"<style\b[^>]*>.*?</style>"," ",text,flags=re.I|re.S)
 text=re.sub(r"<[^>]+>"," ",text[:900000]);return re.sub(r"\s+"," ",htmlmod.unescape(text)).lower()

def classify_page(row,status,final_url,raw_html):
 name=norm(row.get("name"));email=norm(row.get("public_email"));cat=norm(row.get("category"));out=dict(row)
 out.update({"live_status":"UNCERTAIN","http_status":str(status or ""),"final_url":final_url or "","hospitality_hits":"0","identity_hits":"0","email_on_homepage":"NO","live_reason":""})
 out["instagram"]=sanitize_social(row.get("instagram") or "","instagram");out["facebook"]=sanitize_social(row.get("facebook") or "","facebook")
 if status and status>=400:out["live_reason"]=f"HTTP_{status}";return out
 ig=extract_instagram(raw_html,final_url)
 if ig:out["instagram"]=ig;out["instagram_source_url"]=final_url
 text=page_text(raw_html)
 if any(x in text for x in PARKED):out["live_status"]="REJECT";out["live_reason"]="PARKED_OR_FOR_SALE";return out
 if any(x in text for x in CLOSED):out["live_status"]="REJECT";out["live_reason"]="CLOSED_SIGNAL";return out
 explicit_str=any(x in text for x in STR_PROOF);hh=sum(1 for x in HOSP if x in text);it=sum(1 for x in tokens(name) if x in text)
 out["hospitality_hits"]=str(hh);out["identity_hits"]=str(it);out["email_on_homepage"]="YES" if email and email.lower() in text else "NO"
 q=assess_record(out,text,final_url)
 out.update({
  "quality_version":q["quality_version"],"commercial_fit_tier":q["commercial_fit_tier"],"commercial_score":str(q["commercial_score"]),
  "entity_validity_score":str(q["entity_validity_score"]),"premium_score_v2":str(q["premium_score_v2"]),"operator_score_v2":str(q["operator_score_v2"]),
  "sales_ready":"YES" if q["sales_ready"] else "NO","quality_decision":q["quality_decision"],"quality_reason":q["quality_reason"],"premium_signals":q["premium_signals"],
  "destination_signal_count":str(q["destination_signal_count"]),"short_stay_signal_count":str(q["short_stay_signal_count"]),"self_lodging_signal_count":str(q["self_lodging_signal_count"]),
  "invalid_social_count":str(q["invalid_social_count"]),"instagram":q["sanitized_instagram"] or out.get("instagram",''),"facebook":q["sanitized_facebook"]
 })
 out["fit_tier"]=q["commercial_fit_tier"];out["premium_score"]=str(q["premium_score_v2"]);out["operator_score"]=str(q["operator_score_v2"])
 if q["quality_decision"]=="REJECT":out["live_status"]="REJECT";out["live_reason"]="COMMERCIAL_FIT_REJECT:"+q["quality_reason"]
 elif q["quality_decision"]=="REVIEW":out["live_status"]="REVIEW";out["live_reason"]="COMMERCIAL_FIT_REVIEW:"+q["quality_reason"]
 elif hh>=2 and (it>=1 or explicit_str or q["self_lodging_signal_count"]>=1):out["live_status"]="HIGH";out["live_reason"]="CURRENT_HOSPITALITY_IDENTITY_AND_COMMERCIAL_FIT"
 elif hh>=1 and (it>=1 or explicit_str or q["self_lodging_signal_count"]>=1):out["live_status"]="MEDIUM";out["live_reason"]="CURRENT_WEAK_HOSPITALITY_IDENTITY_AND_COMMERCIAL_FIT"
 else:out["live_status"]="REVIEW";out["live_reason"]="INSUFFICIENT_CURRENT_IDENTITY_PROOF"
 return out

def verify(row,timeout):
 url=norm(row.get("website"));base=dict(row);base.update({"live_status":"UNCERTAIN","http_status":"","final_url":"","hospitality_hits":"0","identity_hits":"0","email_on_homepage":"NO","live_reason":""})
 try:
  r=requests.get(url,headers={"User-Agent":UA},timeout=timeout,allow_redirects=True)
  return classify_page(row,r.status_code,r.url,r.text)
 except requests.RequestException as e:base["live_reason"]="NETWORK_"+type(e).__name__.upper();return base

def main():
 ap=argparse.ArgumentParser();ap.add_argument("--input",required=True);ap.add_argument("--outdir",required=True);ap.add_argument("--workers",type=int,default=64);ap.add_argument("--timeout",type=float,default=7.0);a=ap.parse_args();out=Path(a.outdir);out.mkdir(parents=True,exist_ok=True);t0=time.time()
 with open(a.input,encoding="utf-8",newline="") as f:rows=list(csv.DictReader(f))
 verified=[]
 with ThreadPoolExecutor(max_workers=max(1,a.workers)) as ex:
  for fut in as_completed([ex.submit(verify,r,a.timeout) for r in rows]):verified.append(fut.result())
 order={"HIGH":0,"MEDIUM":1,"REVIEW":2,"REJECT":3,"UNCERTAIN":4};verified.sort(key=lambda r:(order.get(r.get("live_status"),9),-(int(r.get("commercial_score") or 0)),r.get("name","").lower()))
 fields=list(verified[0].keys()) if verified else []
 with (out/"v6_live_verified.csv").open("w",encoding="utf-8",newline="") as f:w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(verified)
 keep=[r for r in verified if r.get("live_status") in ("HIGH","MEDIUM") and r.get("fit_tier") in ("A","B") and r.get("sales_ready")=="YES"]
 with (out/"v6_live_ready.csv").open("w",encoding="utf-8",newline="") as f:w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(keep)
 summary={"quality_version":"HOSPITALITY_COMMERCIAL_FIT_V2_1","input_fast_ready":len(rows),"live_high":sum(r.get('live_status')=='HIGH' for r in verified),"live_medium":sum(r.get('live_status')=='MEDIUM' for r in verified),"live_review":sum(r.get('live_status')=='REVIEW' for r in verified),"live_reject":sum(r.get('live_status')=='REJECT' for r in verified),"live_uncertain":sum(r.get('live_status')=='UNCERTAIN' for r in verified),"fit_a":sum(r.get('fit_tier')=='A' for r in verified),"fit_b":sum(r.get('fit_tier')=='B' for r in verified),"fit_c":sum(r.get('fit_tier')=='C' for r in verified),"fit_x":sum(r.get('fit_tier')=='X' for r in verified),"live_ready":len(keep),"instagram_found":sum(bool(r.get('instagram')) for r in keep),"facebook_found":sum(bool(r.get('facebook')) for r in keep),"elapsed_seconds":round(time.time()-t0,2)}
 (out/"v6_live_summary.json").write_text(json.dumps(summary,indent=2)+"\n",encoding="utf-8");print(json.dumps(summary,indent=2))
if __name__=="__main__":main()
