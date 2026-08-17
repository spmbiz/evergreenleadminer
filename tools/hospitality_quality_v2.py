#!/usr/bin/env python3
"""Deterministic Commercial Fit V2 for AI Prod hospitality leads.

Discovery may stay broad. This gate is deliberately conservative for sales use:
A/B = usable target, C = hold/revalidate, X = deterministic reject. Historical
metadata without a current page is never hard-rejected merely for saying
"property management"; a live page is required to prove absence of short-stay.
"""
from __future__ import annotations
import re, unicodedata
from urllib.parse import urlparse

QUALITY_VERSION = "HOSPITALITY_COMMERCIAL_FIT_V2_2"
OTA = {"booking.com","expedia.com","hotels.com","tripadvisor.com","agoda.com","trivago.com","kayak.com","hotelscombined.com","hostelworld.com","hotel.info","holidaycheck.com","planetofhotels.com","booked.net","hotelmix.com","airbnb.com","vrbo.com"}
SOCIAL = {"facebook.com","instagram.com","tiktok.com","youtube.com"}
BAD_DOMAIN_FRAGMENTS = ("hotels-in-","hotel-mix.","hotelmix.","planetofhotels.","booked.")
HOSTED = {"wixsite.com","wordpress.com","linktr.ee","beacons.ai"}
BAD_IG = {"p","reel","reels","stories","explore","accounts","direct","about","legal","developer","privacy","terms","help"}
BAD_FB = {"privacy","login","share","sharer","dialog","plugins","help","policies","terms","settings","watch","marketplace","groups","events"}

STR = (r"\bvacation rentals?\b",r"\bholiday rentals?\b",r"\bholiday homes?\b",r"\bshort[- ]term rentals?\b",r"\bshort stays?\b",r"\bnightly rentals?\b",r"\bserviced apartments?\b",r"\bserviced accommodation\b",r"\baparthotel\b",r"\bvilla rentals?\b",r"\bchalet rentals?\b",r"\bcabin rentals?\b",r"\bdirect bookings?\b")
PROPERTY = (r"\bhotel\b",r"\bresort\b",r"\bvillas?\b",r"\bchalets?\b",r"\bholiday home\b",r"\bvacation home\b",r"\baparthotel\b",r"\bserviced apartment\b",r"\bcottages?\b",r"\bcabins?\b")
SELF_LODGING = (r"\bour hotel\b",r"\bour resort\b",r"\bour rooms\b",r"\bour suites\b",r"\bour cottages\b",r"\bour cabins\b",r"\bour villas\b",r"\brooms?\s*(?:&|and)\s*suites\b",r"\bstay with us\b",r"\bbook (?:a|your) room\b",r"\bcheck availability\b",r"\bhotel guests\b",r"\bfront desk\b",r"\breception\b")
DESTINATION = (r"\bski area\b",r"\bskigebiet\b",r"\bski pass(?:es)?\b",r"\blift pass(?:es)?\b",r"\bski lifts?\b",r"\bcable cars?\b",r"\bpistes?\b",r"\bslopes?\b",r"\btourism region\b",r"\bholiday region\b",r"\btourism association\b",r"\btourist information\b",r"\bvisitor centre\b",r"\ball accommodations\b")
OPERATOR = (r"\bour hotels\b",r"\bour resorts\b",r"\bour properties\b",r"\bour villas\b",r"\bportfolio\b",r"\bmultiple locations\b",r"\bproperty management\b",r"\bvacation rental management\b",r"\bholiday rental management\b")
GENERIC_PM = (r"\bproperty management\b",r"\bproperty manager\b",r"\breal estate management\b")
WEAK = {
 "PENSION_GUESTHOUSE":(r"\bpension\b",r"\bgaestehaus\b",r"\bgastehaus\b",r"\bguest ?house\b"),
 "ORDINARY_INN":(r"\blandgasthof\b",r"\bgasthof\b",r"\bgasthaus\b",r"\binn\b"),
 "BUDGET_LODGING":(r"\bmotel\b",r"\bbudget hotel\b",r"\beconomy hotel\b",r"\bbed\s*(?:and|&)\s*breakfast\b",r"\bb\s*&\s*b\b"),
}
PREMIUM = (
 (r"\b5[- ]star\b|\bfive[- ]star\b|\*\*\*\*\*",25,"5_STAR"),(r"\b4[- ]star superior\b|\bfour[- ]star superior\b|\*\*\*\*\s*superior",22,"4_STAR_SUPERIOR"),(r"\b4[- ]star\b|\bfour[- ]star\b|\*\*\*\*",14,"4_STAR"),
 (r"\bluxur(?:y|ious)\b|\bexclusive\b|\bpremium\b",13,"LUXURY"),(r"\bboutique hotel\b|\bdesign hotel\b|\bboutique resort\b",11,"BOUTIQUE_DESIGN"),(r"\bwellness\b|\bspa\b",10,"SPA_WELLNESS"),
 (r"\binfinity pool\b|\bprivate pool\b|\bpool villa\b",8,"PREMIUM_POOL"),(r"\bsuites?\b|\bpenthouse\b",6,"SUITES"),(r"\bmichelin\b|\bfine dining\b|\bgourmet\b",8,"GASTRONOMY"),(r"\badults[- ]only\b",5,"ADULTS_ONLY"),(r"\bpalace\b|\bchateau\b|\bcastle hotel\b|\bcountry estate\b",8,"PRESTIGE_PROPERTY")
)

def txt(v):
 s=unicodedata.normalize("NFKD",str(v or "")).encode("ascii","ignore").decode().lower().replace("_"," ").replace("&amp;"," and ")
 return re.sub(r"\s+"," ",s).strip()
def host(v):
 s=str(v or "").strip()
 if not s:return ""
 if "://" not in s:s="https://"+s
 try:h=(urlparse(s).hostname or "").lower().strip(".")
 except Exception:return ""
 return h[4:] if h.startswith("www.") else h
def domain_is(h, pool):return any(h==d or h.endswith("."+d) for d in pool)
def blocked_domain(v):
 h=host(v)
 if not h:return "MISSING_DOMAIN"
 if domain_is(h,OTA):return "OTA_OR_DIRECTORY_DOMAIN"
 if domain_is(h,SOCIAL):return "SOCIAL_DOMAIN_NOT_OFFICIAL_SITE"
 if any(x in h for x in BAD_DOMAIN_FRAGMENTS):return "HOTEL_AGGREGATOR_DOMAIN"
 return ""
def hits(s,pats):return sum(bool(re.search(p,s,re.I)) for p in pats)
def anyhit(s,pats):return any(re.search(p,s,re.I) for p in pats)

def sanitize_social(url,platform):
 raw=str(url or "").strip()
 if not raw:return ""
 try:
  p=urlparse(raw if "://" in raw else "https://"+raw);h=(p.hostname or "").lower().strip(".");h=h[4:] if h.startswith("www.") else h;path=(p.path or "").strip("/")
  if platform=="instagram":
   if h!="instagram.com" or not path:return ""
   first=path.split("/",1)[0]
   if first.lower() in BAD_IG or not re.fullmatch(r"[A-Za-z0-9._]{1,30}",first):return ""
   return f"https://www.instagram.com/{first}/"
  if platform=="facebook":
   if h not in {"facebook.com","m.facebook.com"} or not path:return ""
   if path.split("/",1)[0].lower() in BAD_FB:return ""
   return f"https://www.facebook.com/{path.split('?',1)[0].strip('/')}/"
 except Exception:return ""
 return ""

def evidence(row,page_text=""):
 # Never score operational notes (e.g. "cheap-screen") as business evidence.
 keys=("name","category","brand","operator","scraped_title","scraped_text_preview","portfolio_signal","portfolio_evidence","operator_evidence")
 return txt(" ".join([*(str(row.get(k) or "") for k in keys),str(page_text or "")[:900000]]))

def assess_record(row,page_text="",final_url=""):
 name=txt(row.get("name"));category=txt(row.get("category"));ev=evidence(row,page_text);has_live=bool(str(page_text or "").strip());site=final_url or row.get("final_url") or row.get("website") or row.get("domain") or "";drej=blocked_domain(site);reasons=[]
 ig=sanitize_social(row.get("instagram"),"instagram");fb=sanitize_social(row.get("facebook"),"facebook");invalid=0
 if row.get("instagram") and not ig:invalid+=1;reasons.append("INVALID_INSTAGRAM_URL")
 if row.get("facebook") and not fb:invalid+=1;reasons.append("INVALID_FACEBOOK_URL")
 sh=hits(ev,STR);ph=hits(ev,PROPERTY);lh=hits(ev,SELF_LODGING);dh=hits(ev,DESTINATION);generic_pm=anyhit(ev,GENERIC_PM)
 hard=""
 # Deterministic entity types. Shepherd huts/cabins are not mountain refuges.
 if re.search(r"\bhostel\b|\byouth hostel\b|\bbackpacker(?:s)?\b|\bdormitor(?:y|ies)\b|\ba\s*&\s*o\b",name):hard="HOSTEL_OR_DORM"
 elif re.search(r"\b(?:huette|hutte|berghuette|schutzhaus|rifugio|alpine refuge|mountain refuge|mountain hut)\b",name):hard="MOUNTAIN_HUT_OR_REFUGE"
 elif re.search(r"\btourism board\b|\btourist board\b|\btourism association\b|\btourist information\b|\bvisitor(?:s)? centre\b",name):hard="TOURISM_OR_DESTINATION_ENTITY"
 elif re.search(r"\bski area\b|\bskigebiet\b|\bski lifts?\b|\bbergbahnen?\b|\blift company\b",name):hard="SKI_LIFT_ENTITY"
 if drej:reasons.append(drej)
 if hard:reasons.append(hard)
 # Generic PM: historical metadata => hold for live revalidation; current page without STR => reject.
 pm_needs_revalidation=False
 if generic_pm and sh==0:
  if has_live:hard=hard or "GENERIC_PROPERTY_MANAGEMENT_NOT_SHORT_STAY";reasons.append("GENERIC_PROPERTY_MANAGEMENT_NOT_SHORT_STAY")
  else:pm_needs_revalidation=True;reasons.append("GENERIC_PROPERTY_MANAGEMENT_NEEDS_LIVE_STR_REVALIDATION")
 # Destination pages are rejectable from current-page evidence, not merely nearby ski terms.
 if has_live and dh>=3 and lh<2 and sh==0:hard=hard or "DESTINATION_OR_SKI_ENTITY_NOT_LODGING";reasons.append("DESTINATION_OR_SKI_ENTITY_NOT_LODGING")
 # Campground only when it is the primary entity type; incidental camping text is ignored.
 primary=(name+" "+category)
 if re.search(r"\b(?:campground|camping|caravan park|rv park)\b",primary) and "glamping" not in primary and sh==0 and lh==0:
  hard=hard or "NON_TARGET_CAMPGROUND";reasons.append("NON_TARGET_CAMPGROUND")
 # Restaurant/cafe/bar only when category or the business name itself is clearly that entity.
 restaurant_primary=(re.search(r"\b(?:restaurant|cafe)\b",category) or re.match(r"^(?:the\s+)?(?:restaurant|cafe|bar)\b",name))
 if restaurant_primary and sh==0 and lh==0 and ph==0:hard=hard or "RESTAURANT_ONLY";reasons.append("RESTAURANT_ONLY")
 weak=""
 for label,pats in WEAK.items():
  if anyhit(name,pats):weak=label;reasons.append(label);break
 entity=20 + (min(25,10+ph*5) if ph else 0) + (min(35,lh*10) if lh else 0) + (min(40,20+sh*8) if sh else 0)
 try:oldid=int(float(row.get("identity_hits") or 0))
 except Exception:oldid=0
 entity+=min(15,oldid*5)
 if domain_is(host(site),HOSTED):entity-=10;reasons.append("HOSTED_PLATFORM_DOMAIN_REQUIRES_STRONG_IDENTITY")
 if has_live and dh>=2:entity-=min(35,dh*8)
 entity=max(0,min(100,entity))
 premium=25 if ph else 10;signals=[]
 for pat,pts,label in PREMIUM:
  if re.search(pat,ev,re.I):premium+=pts;signals.append(label)
 if re.search(r"\b(?:villa|chalet|private residence|estate|cottage|cabin)\b",ev):premium+=8;signals.append("VISUAL_PROPERTY_TYPE")
 prices=[int(x.replace(",","")) for x in re.findall(r"(?:€|eur|\$|usd|£|gbp)\s*([2-9][0-9]{2,3})",ev,re.I)]
 if prices and max(prices)>=250:premium+=7;signals.append("HIGH_PUBLISHED_RATE")
 if weak:premium-=20
 if re.search(r"\b(?:budget|economy|low[- ]cost)\b",ev):premium-=25;reasons.append("BUDGET_SIGNAL")
 premium=max(0,min(100,premium))
 operator=20+min(55,hits(ev,OPERATOR)*18)
 if re.search(r"\b(?:group|collection|hotels|resorts)\b",name):operator+=20
 if str(row.get("portfolio_signal") or "").strip():operator+=20
 operator=max(0,min(100,operator))
 if hard or drej:tier,ready,decision="X",False,"REJECT"
 elif pm_needs_revalidation:tier,ready,decision="C",False,"REVIEW"
 elif entity<45:tier,ready,decision="C",False,"REVIEW";reasons.append("INSUFFICIENT_ENTITY_IDENTITY")
 elif weak and premium<60:tier,ready,decision="C",False,"REVIEW"
 elif premium>=72 or (premium>=62 and operator>=60):tier,ready,decision="A",True,"ACCEPT"
 elif premium>=50 or sh>=1 or re.search(r"\b(?:villa|chalet|boutique hotel|resort|cottage|cabin)\b",name):tier,ready,decision="B",True,"ACCEPT"
 else:tier,ready,decision="C",False,"REVIEW";reasons.append("ORDINARY_LODGING_NOT_PREMIUM_ENOUGH")
 if domain_is(host(site),HOSTED) and entity<65 and tier in {"A","B"}:tier,ready,decision="C",False,"REVIEW"
 score=max(0,min(100,round(.62*premium+.23*entity+.15*operator)))
 return {"quality_version":QUALITY_VERSION,"commercial_fit_tier":tier,"commercial_score":score,"entity_validity_score":entity,"premium_score_v2":premium,"operator_score_v2":operator,"sales_ready":ready,"quality_decision":decision,"quality_reason":";".join(dict.fromkeys(reasons)) if reasons else "COMMERCIAL_FIT_CONFIRMED","premium_signals":";".join(dict.fromkeys(signals)),"destination_signal_count":dh,"short_stay_signal_count":sh,"self_lodging_signal_count":lh,"sanitized_instagram":ig,"sanitized_facebook":fb,"invalid_social_count":invalid}
