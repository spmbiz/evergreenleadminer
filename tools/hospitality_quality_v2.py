#!/usr/bin/env python3
"""Deterministic commercial-fit gate for AI Prod Hospitality leads.

The acquisition canonical remains append-only evidence. This module decides whether
an observation is commercially suitable for the AI-property-video offer. It is
intentionally stricter than discovery: ambiguous or ordinary lodging is retained
for review/history but is not sales-ready.
"""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlparse

QUALITY_VERSION = "HOSPITALITY_COMMERCIAL_FIT_V2_1"

OTA_DIRECTORY_DOMAINS = {
    "booking.com", "expedia.com", "hotels.com", "tripadvisor.com", "agoda.com",
    "trivago.com", "kayak.com", "hotelscombined.com", "hostelworld.com",
    "hotel.info", "holidaycheck.com", "planetofhotels.com", "booked.net",
    "hotelmix.com", "skyscanner.com", "travelocity.com", "orbitz.com",
    "priceline.com", "traveloka.com", "airbnb.com", "vrbo.com",
}
HOSTED_PLATFORM_DOMAINS = {
    "wixsite.com", "wordpress.com", "lodgify.com", "myguesty.com", "hostaway.com",
    "linktr.ee", "beacons.ai",
}
BAD_DOMAIN_FRAGMENTS = (
    "hotels-in-", "hotel-mix.", "hotelmix.", "planetofhotels.", "booked.",
)
HARD_NAME_PATTERNS = {
    "HOSTEL_OR_DORM": (r"\bhostel\b", r"\byouth hostel\b", r"\bbackpacker(?:s)?\b", r"\bdormitor(?:y|ies)\b", r"\ba\s*&\s*o\b"),
    "MOUNTAIN_HUT_OR_REFUGE": (r"\bhuette\b", r"\bhutte\b", r"\bhut\b", r"\bhuts\b", r"\bberghuette\b", r"\bschutzhaus\b", r"\balpenverein\b", r"\brefuge\b", r"\brifugio\b"),
    "TOURISM_OR_DESTINATION_ENTITY": (r"\btourism board\b", r"\btourist board\b", r"\btourism association\b", r"\btourist information\b", r"\bvisitor(?:s)? centre\b", r"\bdestination management\b"),
    "SKI_LIFT_ENTITY": (r"\bski area\b", r"\bskigebiet\b", r"\bski lifts?\b", r"\blift company\b", r"\bbergbahnen?\b", r"\bcable car\b"),
}
WEAK_TYPE_PATTERNS = {
    "PENSION_GUESTHOUSE": (r"\bpension\b", r"\bgaestehaus\b", r"\bgastehaus\b", r"\bguest ?house\b"),
    "ORDINARY_INN": (r"\blandgasthof\b", r"\bgasthof\b", r"\bgasthaus\b", r"\binn\b"),
    "BUDGET_LODGING": (r"\bmotel\b", r"\bbudget hotel\b", r"\beconomy hotel\b", r"\bbed\s*(?:and|&)\s*breakfast\b", r"\bb\s*&\s*b\b"),
}
PREMIUM_RULES = (
    (r"\b5[- ]star\b|\bfive[- ]star\b|\*\*\*\*\*", 25, "5_STAR"),
    (r"\b4[- ]star superior\b|\bfour[- ]star superior\b|\*\*\*\*\s*superior", 22, "4_STAR_SUPERIOR"),
    (r"\b4[- ]star\b|\bfour[- ]star\b|\*\*\*\*", 14, "4_STAR"),
    (r"\bluxur(?:y|ious)\b|\bexclusive\b|\bpremium\b", 13, "LUXURY"),
    (r"\bboutique hotel\b|\bdesign hotel\b|\bboutique resort\b", 11, "BOUTIQUE_DESIGN"),
    (r"\bwellness\b|\bspa\b", 10, "SPA_WELLNESS"),
    (r"\binfinity pool\b|\bprivate pool\b|\bpool villa\b", 8, "PREMIUM_POOL"),
    (r"\bsuites?\b|\bpenthouse\b", 6, "SUITES"),
    (r"\bmichelin\b|\bfine dining\b|\bgourmet\b", 8, "GASTRONOMY"),
    (r"\badults[- ]only\b|\badults only\b", 5, "ADULTS_ONLY"),
    (r"\baward[- ]winning\b|\baward winning\b", 4, "AWARD"),
    (r"\bpalace\b|\bchateau\b|\bcastle hotel\b|\bcountry estate\b", 8, "PRESTIGE_PROPERTY"),
)
SELF_LODGING_PATTERNS = (r"\bour hotel\b", r"\bour resort\b", r"\bour rooms\b", r"\brooms?\s*(?:&|and)\s*suites\b", r"\bour suites\b", r"\bhotel guests\b", r"\bstay with us\b", r"\bbook (?:a|your) room\b", r"\broom rates\b", r"\bcheck availability\b", r"\breception\b", r"\bfront desk\b", r"\bspa hotel\b", r"\bwellness hotel\b")
STR_PATTERNS = (r"\bvacation rentals?\b", r"\bholiday rentals?\b", r"\bholiday homes?\b", r"\bshort[- ]term rentals?\b", r"\bshort stays?\b", r"\bnightly rentals?\b", r"\bserviced apartments?\b", r"\bserviced accommodation\b", r"\baparthotel\b", r"\bvilla rentals?\b", r"\bchalet rentals?\b", r"\bcabin rentals?\b")
PROPERTY_TYPE_PATTERNS = (r"\bhotel\b", r"\bresort\b", r"\bvilla\b", r"\bvillas\b", r"\bchalet\b", r"\bholiday home\b", r"\bvacation home\b", r"\baparthotel\b", r"\bserviced apartment\b")
DESTINATION_PAGE_PATTERNS = (r"\bski area\b", r"\bskigebiet\b", r"\bski pass(?:es)?\b", r"\blift pass(?:es)?\b", r"\bski lifts?\b", r"\bcable cars?\b", r"\bpistes?\b", r"\bslopes?\b", r"\btourism region\b", r"\bholiday region\b", r"\btourism association\b", r"\btourist information\b", r"\bvisitor centre\b", r"\bthings to do\b", r"\baccommodations? in (?:the|our) region\b", r"\ball accommodations\b")
OPERATOR_PATTERNS = (r"\bour hotels\b", r"\bour resorts\b", r"\bour properties\b", r"\bour villas\b", r"\bportfolio\b", r"\bmultiple locations\b", r"\bproperty management\b", r"\bvacation rental management\b", r"\bholiday rental management\b")
GENERIC_PM_PATTERNS = (r"\bproperty management\b", r"\bproperty manager\b", r"\breal estate management\b")
BAD_INSTAGRAM_PREFIXES = {"p","reel","reels","stories","explore","accounts","direct","about","legal","developer","privacy","terms","help"}
BAD_FACEBOOK_PREFIXES = {"privacy","login","share","sharer","dialog","plugins","help","policies","terms","settings","watch","marketplace","groups","events"}

def ascii_text(value: str) -> str:
    s = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    s = s.lower().replace("&amp;", " and ").replace("_", " ")
    return re.sub(r"\s+", " ", s).strip()

def host_of(value: str) -> str:
    s=str(value or '').strip()
    if not s:return ''
    if '://' not in s:s='https://'+s
    try:h=(urlparse(s).hostname or '').lower().strip('.')
    except Exception:return ''
    return h[4:] if h.startswith('www.') else h

def _domain_matches(host: str, domains: set[str]) -> bool:
    h=host_of(host) if '://' in str(host or '') else str(host or '').lower().strip('.')
    if h.startswith('www.'):h=h[4:]
    return any(h==d or h.endswith('.'+d) for d in domains)

def blocked_official_domain(value: str) -> str:
    d=host_of(value) if value else ''
    if not d:return 'MISSING_DOMAIN'
    if _domain_matches(d,OTA_DIRECTORY_DOMAINS):return 'OTA_OR_DIRECTORY_DOMAIN'
    if _domain_matches(d,{"facebook.com","instagram.com","tiktok.com","youtube.com"}):return 'SOCIAL_DOMAIN_NOT_OFFICIAL_SITE'
    if any(fragment in d for fragment in BAD_DOMAIN_FRAGMENTS):return 'HOTEL_AGGREGATOR_DOMAIN'
    return ''

def hosted_platform_domain(value: str) -> bool:return _domain_matches(value,HOSTED_PLATFORM_DOMAINS)
def _path_match(text,patterns):return any(re.search(p,text,flags=re.I) for p in patterns)
def _count_patterns(text,patterns):return sum(1 for p in patterns if re.search(p,text,flags=re.I))

def sanitize_social(url: str, platform: str) -> str:
    raw=str(url or '').strip()
    if not raw:return ''
    try:
        p=urlparse(raw if '://' in raw else 'https://'+raw);h=(p.hostname or '').lower().strip('.')
        if h.startswith('www.'):h=h[4:]
        path=(p.path or '').strip('/')
        if platform=='instagram':
            if h!='instagram.com' or not path:return ''
            first=path.split('/',1)[0].lower()
            if first in BAD_INSTAGRAM_PREFIXES or not re.fullmatch(r'[A-Za-z0-9._]{1,30}',path.split('/',1)[0]):return ''
            return f"https://www.instagram.com/{path.split('/',1)[0]}/"
        if platform=='facebook':
            if h not in {'facebook.com','m.facebook.com'} or not path:return ''
            first=path.split('/',1)[0].lower()
            if first in BAD_FACEBOOK_PREFIXES:return ''
            clean=path.split('?',1)[0].strip('/');return f'https://www.facebook.com/{clean}/'
    except Exception:return ''
    return ''

def combined_evidence(row: dict, page_text: str = '') -> str:
    # Operational notes are deliberately excluded: terms such as "cheap-screen"
    # describe pipeline mechanics, not the business or property.
    keys=("name","category","brand","operator","scraped_title","scraped_text_preview","portfolio_signal","portfolio_evidence","operator_evidence")
    vals=[str(row.get(k) or '') for k in keys]
    if page_text:vals.append(page_text[:900000])
    return ascii_text(' '.join(vals))

def _hard_name_reason(name_text):
    for reason,patterns in HARD_NAME_PATTERNS.items():
        if _path_match(name_text,patterns):return reason
    return ''
def _weak_reason(name_text):
    for reason,patterns in WEAK_TYPE_PATTERNS.items():
        if _path_match(name_text,patterns):return reason
    return ''

def assess_record(row: dict, page_text: str = '', final_url: str = '') -> dict:
    name_text=ascii_text(str(row.get('name') or ''));evidence=combined_evidence(row,page_text);website=final_url or row.get('final_url') or row.get('website') or row.get('domain') or '';domain_reason=blocked_official_domain(website);reasons=[]
    instagram=sanitize_social(row.get('instagram') or '','instagram');facebook=sanitize_social(row.get('facebook') or '','facebook');invalid_socials=0
    if row.get('instagram') and not instagram:invalid_socials+=1;reasons.append('INVALID_INSTAGRAM_URL')
    if row.get('facebook') and not facebook:invalid_socials+=1;reasons.append('INVALID_FACEBOOK_URL')
    hard=_hard_name_reason(name_text)
    if hard:reasons.append(hard)
    if domain_reason:reasons.append(domain_reason)
    str_hits=_count_patterns(evidence,STR_PATTERNS);property_hits=_count_patterns(evidence,PROPERTY_TYPE_PATTERNS);self_lodging_hits=_count_patterns(evidence,SELF_LODGING_PATTERNS);destination_hits=_count_patterns(evidence,DESTINATION_PAGE_PATTERNS);generic_pm=_path_match(evidence,GENERIC_PM_PATTERNS)
    if generic_pm and str_hits==0:hard=hard or 'GENERIC_PROPERTY_MANAGEMENT_NOT_SHORT_STAY';reasons.append('GENERIC_PROPERTY_MANAGEMENT_NOT_SHORT_STAY')
    if destination_hits>=3 and self_lodging_hits<2 and str_hits==0:hard=hard or 'DESTINATION_OR_SKI_ENTITY_NOT_LODGING';reasons.append('DESTINATION_OR_SKI_ENTITY_NOT_LODGING')
    if re.search(r'\b(?:camping|campground|caravan park|rv park)\b',evidence) and 'glamping' not in evidence:hard=hard or 'NON_TARGET_CAMPGROUND';reasons.append('NON_TARGET_CAMPGROUND')
    restaurant_only=_path_match(name_text,(r'\brestaurant\b',r'\bcafe\b',r'\bbar\b'))
    if restaurant_only and self_lodging_hits==0 and property_hits==0 and str_hits==0:hard=hard or 'RESTAURANT_ONLY';reasons.append('RESTAURANT_ONLY')
    weak=_weak_reason(name_text)
    if weak:reasons.append(weak)
    entity=20
    if property_hits:entity+=min(25,10+property_hits*5)
    if self_lodging_hits:entity+=min(35,self_lodging_hits*10)
    if str_hits:entity+=min(40,20+str_hits*8)
    try:old_identity_hits=int(float(row.get('identity_hits') or 0))
    except Exception:old_identity_hits=0
    if old_identity_hits:entity+=min(15,old_identity_hits*5)
    if hosted_platform_domain(website):entity-=10;reasons.append('HOSTED_PLATFORM_DOMAIN_REQUIRES_STRONG_IDENTITY')
    if destination_hits>=2:entity-=min(35,destination_hits*8)
    entity=max(0,min(100,entity))
    premium=25 if property_hits else 10;premium_signals=[]
    for pattern,points,label in PREMIUM_RULES:
        if re.search(pattern,evidence,flags=re.I):premium+=points;premium_signals.append(label)
    if re.search(r'\b(?:villa|chalet|private residence|estate)\b',evidence):premium+=8;premium_signals.append('VISUAL_PROPERTY_TYPE')
    prices=[int(x.replace(',','')) for x in re.findall(r'(?:€|eur|\$|usd|£|gbp)\s*([2-9][0-9]{2,3})',evidence,flags=re.I)]
    if prices and max(prices)>=250:premium+=7;premium_signals.append('HIGH_PUBLISHED_RATE')
    if weak:premium-=20
    if re.search(r'\b(?:budget|economy|low[- ]cost)\b',evidence):premium-=25;reasons.append('BUDGET_SIGNAL')
    premium=max(0,min(100,premium))
    operator=20;operator_hits=_count_patterns(evidence,OPERATOR_PATTERNS);operator+=min(55,operator_hits*18)
    if re.search(r'\b(?:group|collection|hotels|resorts)\b',name_text):operator+=20
    if str(row.get('portfolio_signal') or '').strip():operator+=20
    operator=max(0,min(100,operator))
    if hard or domain_reason:tier,sales_ready,decision='X',False,'REJECT'
    elif entity<45:tier,sales_ready,decision='C',False,'REVIEW';reasons.append('INSUFFICIENT_ENTITY_IDENTITY')
    elif weak and premium<60:tier,sales_ready,decision='C',False,'REVIEW'
    elif premium>=72 or (premium>=62 and operator>=60):tier,sales_ready,decision='A',True,'ACCEPT'
    elif premium>=50 or str_hits>=1 or re.search(r'\b(?:villa|chalet|boutique hotel|resort)\b',name_text):tier,sales_ready,decision='B',True,'ACCEPT'
    else:tier,sales_ready,decision='C',False,'REVIEW';reasons.append('ORDINARY_LODGING_NOT_PREMIUM_ENOUGH')
    if hosted_platform_domain(website) and entity<65 and tier in {'A','B'}:tier,sales_ready,decision='C',False,'REVIEW'
    commercial_score=max(0,min(100,round(.62*premium+.23*entity+.15*operator)))
    return {'quality_version':QUALITY_VERSION,'commercial_fit_tier':tier,'commercial_score':commercial_score,'entity_validity_score':entity,'premium_score_v2':premium,'operator_score_v2':operator,'sales_ready':sales_ready,'quality_decision':decision,'quality_reason':';'.join(dict.fromkeys(reasons)) if reasons else 'COMMERCIAL_FIT_CONFIRMED','premium_signals':';'.join(dict.fromkeys(premium_signals)),'destination_signal_count':destination_hits,'short_stay_signal_count':str_hits,'self_lodging_signal_count':self_lodging_hits,'sanitized_instagram':instagram,'sanitized_facebook':facebook,'invalid_social_count':invalid_socials}
