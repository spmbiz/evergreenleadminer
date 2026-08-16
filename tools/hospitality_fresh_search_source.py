#!/usr/bin/env python3
"""Fresh Hospitality discovery source using the existing SearchFabric.

This adapter searches a deterministic long-tail catalog of premium operator,
PMS and direct-booking queries. It rejects canonical-known/portal/social
domains before expensive enrichment. It never writes canonical state; the
single-writer aggregate remains authoritative.
"""
from __future__ import annotations
import argparse, csv, gzip, hashlib, json, math, re, time
from pathlib import Path
from urllib.parse import urlparse
from hospitality_search_fabric import SearchFabric

ROOT = Path(__file__).resolve().parents[1]
CFG = ROOT / "config/hospitality_fresh_search_sources.json"
FIELDS = ["source","source_family","source_release","source_record_id","osm_type","osm_id",
"country","region","name","category","brand","operator","website","domain","public_email",
"email_domain","email_domain_match","public_phone","city","state","street","confidence",
"operator_score","premium_score","fit_tier","source_url","overture_id","notes","instagram","facebook"]
MULTI = ("co.uk","org.uk","me.uk","ltd.uk","plc.uk","net.uk","com.au","net.au","org.au",
"com.br","com.mx","co.nz","net.nz","org.nz","co.za","com.pt","com.es","com.tr","co.jp",
"com.sg","com.hk","com.my")

# Structured evidence: id|label|city|region|country|country_code.
# Western/premium weighted for the current commercial ICP.
BUILTIN_MARKETS_RAW = """orlando-fl|Orlando Florida|Orlando|Florida|United States|US
kissimmee-fl|Kissimmee Florida|Kissimmee|Florida|United States|US
miami-fl|Miami Florida|Miami|Florida|United States|US
miami-beach-fl|Miami Beach Florida|Miami Beach|Florida|United States|US
fort-lauderdale-fl|Fort Lauderdale Florida|Fort Lauderdale|Florida|United States|US
palm-beach-fl|Palm Beach Florida|Palm Beach|Florida|United States|US
naples-fl|Naples Florida|Naples|Florida|United States|US
marco-island-fl|Marco Island Florida|Marco Island|Florida|United States|US
key-west-fl|Key West Florida|Key West|Florida|United States|US
destin-fl|Destin Florida|Destin|Florida|United States|US
30a-fl|30A Florida|30A|Florida|United States|US
anna-maria-island-fl|Anna Maria Island Florida|Anna Maria Island|Florida|United States|US
st-pete-beach-fl|St Pete Beach Florida|St Pete Beach|Florida|United States|US
clearwater-beach-fl|Clearwater Beach Florida|Clearwater Beach|Florida|United States|US
palm-springs-ca|Palm Springs California|Palm Springs|California|United States|US
san-diego-ca|San Diego California|San Diego|California|United States|US
malibu-ca|Malibu California|Malibu|California|United States|US
santa-barbara-ca|Santa Barbara California|Santa Barbara|California|United States|US
napa-ca|Napa California|Napa|California|United States|US
sonoma-ca|Sonoma California|Sonoma|California|United States|US
lake-tahoe-ca|Lake Tahoe California|Lake Tahoe|California|United States|US
mammoth-lakes-ca|Mammoth Lakes California|Mammoth Lakes|California|United States|US
big-bear-ca|Big Bear California|Big Bear|California|United States|US
joshua-tree-ca|Joshua Tree California|Joshua Tree|California|United States|US
scottsdale-az|Scottsdale Arizona|Scottsdale|Arizona|United States|US
sedona-az|Sedona Arizona|Sedona|Arizona|United States|US
park-city-ut|Park City Utah|Park City|Utah|United States|US
moab-ut|Moab Utah|Moab|Utah|United States|US
aspen-co|Aspen Colorado|Aspen|Colorado|United States|US
vail-co|Vail Colorado|Vail|Colorado|United States|US
breckenridge-co|Breckenridge Colorado|Breckenridge|Colorado|United States|US
telluride-co|Telluride Colorado|Telluride|Colorado|United States|US
steamboat-springs-co|Steamboat Springs Colorado|Steamboat Springs|Colorado|United States|US
jackson-hole-wy|Jackson Hole Wyoming|Jackson Hole|Wyoming|United States|US
nashville-tn|Nashville Tennessee|Nashville|Tennessee|United States|US
gatlinburg-tn|Gatlinburg Tennessee|Gatlinburg|Tennessee|United States|US
pigeon-forge-tn|Pigeon Forge Tennessee|Pigeon Forge|Tennessee|United States|US
austin-tx|Austin Texas|Austin|Texas|United States|US
charleston-sc|Charleston South Carolina|Charleston|South Carolina|United States|US
hilton-head-sc|Hilton Head South Carolina|Hilton Head|South Carolina|United States|US
myrtle-beach-sc|Myrtle Beach South Carolina|Myrtle Beach|South Carolina|United States|US
outer-banks-nc|Outer Banks North Carolina|Outer Banks|North Carolina|United States|US
asheville-nc|Asheville North Carolina|Asheville|North Carolina|United States|US
savannah-ga|Savannah Georgia|Savannah|Georgia|United States|US
hamptons-ny|The Hamptons New York|The Hamptons|New York|United States|US
cape-cod-ma|Cape Cod Massachusetts|Cape Cod|Massachusetts|United States|US
nantucket-ma|Nantucket Massachusetts|Nantucket|Massachusetts|United States|US
marthas-vineyard-ma|Martha's Vineyard Massachusetts|Martha's Vineyard|Massachusetts|United States|US
newport-ri|Newport Rhode Island|Newport|Rhode Island|United States|US
bar-harbor-me|Bar Harbor Maine|Bar Harbor|Maine|United States|US
whistler-bc|Whistler British Columbia|Whistler|British Columbia|Canada|CA
muskoka-on|Muskoka Ontario|Muskoka|Ontario|Canada|CA
canmore-ab|Canmore Alberta|Canmore|Alberta|Canada|CA
banff-ab|Banff Alberta|Banff|Alberta|Canada|CA
mont-tremblant-qc|Mont Tremblant Quebec|Mont Tremblant|Quebec|Canada|CA
tofino-bc|Tofino British Columbia|Tofino|British Columbia|Canada|CA
mallorca-es|Mallorca Spain|Mallorca|Balearic Islands|Spain|ES
ibiza-es|Ibiza Spain|Ibiza|Balearic Islands|Spain|ES
menorca-es|Menorca Spain|Menorca|Balearic Islands|Spain|ES
marbella-es|Marbella Spain|Marbella|Andalusia|Spain|ES
costa-del-sol-es|Costa del Sol Spain|Costa del Sol|Andalusia|Spain|ES
costa-brava-es|Costa Brava Spain|Costa Brava|Catalonia|Spain|ES
tenerife-es|Tenerife Spain|Tenerife|Canary Islands|Spain|ES
lanzarote-es|Lanzarote Spain|Lanzarote|Canary Islands|Spain|ES
algarve-pt|Algarve Portugal|Algarve|Algarve|Portugal|PT
cascais-pt|Cascais Portugal|Cascais|Lisbon|Portugal|PT
comporta-pt|Comporta Portugal|Comporta|Setubal|Portugal|PT
madeira-pt|Madeira Portugal|Madeira|Madeira|Portugal|PT
french-riviera-fr|French Riviera France|French Riviera|Provence-Alpes-Cote d'Azur|France|FR
saint-tropez-fr|Saint Tropez France|Saint Tropez|Provence-Alpes-Cote d'Azur|France|FR
cannes-fr|Cannes France|Cannes|Provence-Alpes-Cote d'Azur|France|FR
nice-fr|Nice France|Nice|Provence-Alpes-Cote d'Azur|France|FR
provence-fr|Provence France|Provence|Provence-Alpes-Cote d'Azur|France|FR
corsica-fr|Corsica France|Corsica|Corsica|France|FR
chamonix-fr|Chamonix France|Chamonix|Auvergne-Rhone-Alpes|France|FR
megeve-fr|Megeve France|Megeve|Auvergne-Rhone-Alpes|France|FR
courchevel-fr|Courchevel France|Courchevel|Auvergne-Rhone-Alpes|France|FR
tuscany-it|Tuscany Italy|Tuscany|Tuscany|Italy|IT
lake-como-it|Lake Como Italy|Lake Como|Lombardy|Italy|IT
amalfi-coast-it|Amalfi Coast Italy|Amalfi Coast|Campania|Italy|IT
puglia-it|Puglia Italy|Puglia|Apulia|Italy|IT
sardinia-it|Sardinia Italy|Sardinia|Sardinia|Italy|IT
sicily-it|Sicily Italy|Sicily|Sicily|Italy|IT
dolomites-it|Dolomites Italy|Dolomites|Trentino-Alto Adige|Italy|IT
mykonos-gr|Mykonos Greece|Mykonos|South Aegean|Greece|GR
santorini-gr|Santorini Greece|Santorini|South Aegean|Greece|GR
crete-gr|Crete Greece|Crete|Crete|Greece|GR
corfu-gr|Corfu Greece|Corfu|Ionian Islands|Greece|GR
paros-gr|Paros Greece|Paros|South Aegean|Greece|GR
dubrovnik-hr|Dubrovnik Croatia|Dubrovnik|Dubrovnik-Neretva|Croatia|HR
hvar-hr|Hvar Croatia|Hvar|Split-Dalmatia|Croatia|HR
split-hr|Split Croatia|Split|Split-Dalmatia|Croatia|HR
istria-hr|Istria Croatia|Istria|Istria|Croatia|HR
zermatt-ch|Zermatt Switzerland|Zermatt|Valais|Switzerland|CH
verbier-ch|Verbier Switzerland|Verbier|Valais|Switzerland|CH
st-moritz-ch|St Moritz Switzerland|St Moritz|Graubunden|Switzerland|CH
gstaad-ch|Gstaad Switzerland|Gstaad|Bern|Switzerland|CH
kitzbuhel-at|Kitzbuhel Austria|Kitzbuhel|Tyrol|Austria|AT
tyrol-at|Tyrol Austria|Tyrol|Tyrol|Austria|AT
salzburg-alps-at|Salzburg Alps Austria|Salzburg Alps|Salzburg|Austria|AT
cotswolds-uk|Cotswolds England|Cotswolds|England|United Kingdom|GB
cornwall-uk|Cornwall England|Cornwall|England|United Kingdom|GB
lake-district-uk|Lake District England|Lake District|England|United Kingdom|GB
scottish-highlands-uk|Scottish Highlands Scotland|Scottish Highlands|Scotland|United Kingdom|GB
gold-coast-au|Gold Coast Australia|Gold Coast|Queensland|Australia|AU
byron-bay-au|Byron Bay Australia|Byron Bay|New South Wales|Australia|AU
noosa-au|Noosa Australia|Noosa|Queensland|Australia|AU
mornington-peninsula-au|Mornington Peninsula Australia|Mornington Peninsula|Victoria|Australia|AU
queenstown-nz|Queenstown New Zealand|Queenstown|Otago|New Zealand|NZ
wanaka-nz|Wanaka New Zealand|Wanaka|Otago|New Zealand|NZ"""

EXTRA_FAMILIES = [('short_stay_manager', 86, 68, '"short stay management" {market} rentals'), ('vacation_home_agency', 82, 72, '"vacation home agency" {market}'), ('luxury_home_rentals', 76, 86, '"luxury home rentals" {market}'), ('villa_rental_company', 82, 82, '"villa rental company" {market}'), ('holiday_let_management', 84, 66, '"holiday let management" {market}'), ('vacation_property_management', 88, 70, '"vacation property management" {market}'), ('luxury_vacation_company', 80, 84, '"luxury vacation rentals" {market} company'), ('direct_vacation_homes', 72, 76, '"vacation homes" {market} "book direct"'), ('direct_holiday_rentals', 72, 76, '"holiday rentals" {market} "book direct"'), ('property_manager_villas', 88, 76, '"property manager" {market} villas rentals'), ('villa_hosts', 76, 78, '"villa hosts" {market} rentals'), ('vacation_rental_group', 84, 72, '"vacation rental group" {market}'), ('hospitality_management_villas', 84, 78, '"hospitality management" {market} villas'), ('luxury_lodging_direct', 66, 82, '"luxury lodging" {market} "book direct"'), ('private_homes_collection', 76, 82, '"private homes" {market} "collection" rentals'), ('curated_villas', 72, 88, '"curated villas" {market} rentals'), ('managed_vacation_rentals', 86, 70, '"managed vacation rentals" {market}'), ('premium_short_stay', 80, 78, '"premium short stay" {market} rentals'), ('local_vacation_manager', 88, 66, '"local vacation rental management" {market}'), ('owner_direct_portfolio', 84, 72, '"vacation rentals" {market} "owner direct"')]
QUERY_LENSES = (("core", ""),("no_ota", " -airbnb -booking -vrbo -tripadvisor"),("official", ' "official site"'),("contact", ' "contact us"'),("portfolio", ' "our properties"'),("direct", ' "book direct"'))


def load_json(path: Path) -> dict: return json.loads(path.read_text(encoding="utf-8"))
def root_host(host: str) -> str:
    h=(host or "").lower().strip(".")
    if h.startswith("www."): h=h[4:]
    if not h: return ""
    for suffix in MULTI:
        if h.endswith("."+suffix): return ".".join(h.split(".")[-3:])
    p=h.split("."); return ".".join(p[-2:]) if len(p)>=2 else h

def domain_of(url: str) -> str:
    try: return root_host(urlparse(url).hostname or "")
    except Exception: return ""

def read_domains(path: str) -> set[str]:
    if not path or not Path(path).exists(): return set()
    p=Path(path); opener=gzip.open if p.suffix==".gz" else open
    with opener(p,"rt",encoding="utf-8") as f: return {root_host(x.strip()) for x in f if x.strip()}

def clean_title(title: str, domain: str) -> str:
    t=re.sub(r"\s+"," ",title or "").strip(); parts=re.split(r"\s+[|–—]\s+|\s+-\s+",t); name=(parts[0] if parts else t).strip()
    return domain if not name or name.lower() in {"home","official site","book direct","vacation rentals","luxury villas"} else name[:180]

def builtin_markets() -> list[dict]:
    out=[]
    for line in BUILTIN_MARKETS_RAW.splitlines():
        bits=line.split("|")
        if len(bits)!=6: continue
        i,label,city,region,country,cc=bits
        out.append({"id":i,"label":label,"city":city,"region":region,"state":region,"country":country,"country_code":cc.upper()})
    return out

def normalize_market(item) -> dict:
    if isinstance(item,dict):
        label=str(item.get("label") or item.get("market") or item.get("city") or "").strip()
        return {"id":str(item.get("id") or label).strip(),"label":label,"city":str(item.get("city") or "").strip(),"region":str(item.get("region") or item.get("state") or "").strip(),"state":str(item.get("state") or item.get("region") or "").strip(),"country":str(item.get("country") or "").strip(),"country_code":str(item.get("country_code") or "").strip().upper()}
    label=str(item or "").strip(); return {"id":label,"label":label,"city":"","region":"","state":"","country":"","country_code":""}

def family_rows(cfg: dict) -> list[dict]:
    rows=[dict(f) for f in (cfg.get("query_families") or []) if f.get("template")]
    rows += [{"id":i,"operator_score":op,"premium_score":premium,"template":template} for i,op,premium,template in EXTRA_FAMILIES]
    seen=set(); out=[]
    for fam in rows:
        fid=str(fam.get("id") or "search")
        if fid in seen: continue
        seen.add(fid); out.append(fam)
    return out

def build_specs(cfg: dict) -> list[dict]:
    markets=[]; seen_market=set()
    for item in builtin_markets()+list(cfg.get("markets") or []):
        m=normalize_market(item); key=m["label"].lower()
        if not key or key in seen_market: continue
        seen_market.add(key); markets.append(m)
    specs=[]; seen_query=set()
    for market in markets:
        for fam in family_rows(cfg):
            base=str(fam.get("template") or "").strip()
            if not base: continue
            for lens_id,suffix in QUERY_LENSES:
                try: q=(base+suffix).format(market=market["label"],city=market["city"],region=market["region"],state=market["state"],country=market["country"],country_code=market["country_code"]).strip()
                except Exception: continue
                qkey=re.sub(r"\s+"," ",q).lower().strip()
                if not qkey or qkey in seen_query: continue
                seen_query.add(qkey); specs.append({"family":f'{fam.get("id") or "search"}__{lens_id}',"market":market,"family_spec":fam,"query":q})
    return specs

def write_csv(path: Path, rows: list[dict]):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open("w",encoding="utf-8",newline="") as f:
        w=csv.DictWriter(f,fieldnames=FIELDS,extrasaction="ignore"); w.writeheader(); w.writerows(rows)

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--config",default=str(CFG)); ap.add_argument("--canonical-domains",default=""); ap.add_argument("--outdir",required=True); ap.add_argument("--max-queries",type=int,default=0); ap.add_argument("--cursor",type=int,default=0); args=ap.parse_args()
    cfg=load_json(Path(args.config))
    if not cfg.get("enabled"): raise SystemExit("fresh-search source disabled")
    policy=cfg.get("policy") or {}; max_queries=args.max_queries or int(policy.get("max_queries_per_canary") or 30); max_results=int(policy.get("max_results_per_query") or 8); canonical=read_domains(args.canonical_domains); excluded={root_host(x) for x in (cfg.get("excluded_domains") or []) if root_host(x)}
    catalog=build_specs(cfg); total=len(catalog); bootstrap_span=max(1,len(cfg.get("markets") or [])*len(cfg.get("query_families") or [])); epoch_minutes=max(1.0,float(policy.get("rotation_epoch_minutes") or 5)); epoch=int(time.time()//(epoch_minutes*60)); windows=max(1,math.ceil(total/bootstrap_span)) if total else 1; epoch_window=epoch%windows; effective_cursor=(int(args.cursor)+epoch_window*bootstrap_span)%total if total else 0; specs=(catalog[effective_cursor:]+catalog[:effective_cursor])[:max(0,max_queries)] if catalog else []
    fabric=SearchFabric({"timeout_seconds":float(policy.get("timeout_seconds") or 12),"max_results":max_results,"ddgs_enabled":True,"openserp_url":"","searxng_url":"","provider_delay_seconds":float(policy.get("provider_delay_seconds") or 0),"stop_after_first_successful_provider":bool(policy.get("stop_after_first_successful_provider",True))})
    out=Path(args.outdir); out.mkdir(parents=True,exist_ok=True); observations=[]; events=[]; candidates={}; raw_results=excluded_portal=canonical_known=duplicate_domain=0; t0=time.time()
    for spec in specs:
        family=spec["family"]; market=spec["market"]; fam=spec["family_spec"]; query=spec["query"]; results,ev=fabric.search(family,query); events.extend([{**e,"market_id":market["id"],"market":market["label"],"query_family":family,"query":query} for e in ev]); raw_results+=len(results)
        for r in results:
            url=str(r.get("url") or "").strip(); domain=domain_of(url); observations.append({**r,"market_id":market["id"],"market":market["label"],"country":market["country"],"query_family":family,"query":query,"domain":domain})
            if not domain or domain in excluded or any(domain.endswith("."+x) for x in excluded): excluded_portal+=1; continue
            if domain in canonical: canonical_known+=1; continue
            if domain in candidates: duplicate_domain+=1; continue
            op=int(fam.get("operator_score") or 60); premium=int(fam.get("premium_score") or 60); rid=hashlib.sha256(f'{family}|{market["id"]}|{domain}'.encode()).hexdigest()[:20]
            candidates[domain]={"source":"SearchFabric fresh-search","source_family":"search_fabric_fresh","source_release":"fresh-v3","source_record_id":f"fresh:{rid}","osm_type":"","osm_id":"","country":market["country"],"region":market["region"],"name":clean_title(str(r.get("title") or ""),domain),"category":"hospitality_search_candidate","brand":"","operator":"","website":url,"domain":domain,"public_email":"","email_domain":"","email_domain_match":"","public_phone":"","city":market["city"],"state":market["state"],"street":"","confidence":"PUBLIC_SEARCH_RESULT_UNSEEN_DOMAIN","operator_score":str(op),"premium_score":str(premium),"fit_tier":"A" if op>=75 or premium>=78 else "B","source_url":url,"overture_id":"","notes":f'Public search evidence; market_id={market["id"]}; family={family}; provider={r.get("provider")}; rank={r.get("rank")}; query={query}',"instagram":"","facebook":""}
    rows=list(candidates.values()); write_csv(out/"v6_recovery_candidates.csv",rows)
    with (out/"source_observations.jsonl").open("w",encoding="utf-8") as f:
        for row in observations: f.write(json.dumps(row,ensure_ascii=False,separators=(",",":"))+"\n")
    summary={"schema":"HOSPITALITY_FRESH_SEARCH_V3","catalog_queries_total":total,"bootstrap_span":bootstrap_span,"rotation_epoch_minutes":epoch_minutes,"rotation_window":epoch_window,"requested_cursor":int(args.cursor),"effective_cursor":effective_cursor,"queries_planned":len(specs),"queries_with_provider_ok":sum(1 for e in events if e.get("status")=="OK"),"provider_events":len(events),"raw_search_results":raw_results,"excluded_portal_or_social":excluded_portal,"canonical_known_rejected_early":canonical_known,"duplicate_domain_results":duplicate_domain,"canonical_unseen_candidate_domains":len(rows),"canonical_snapshot_domains":len(canonical),"elapsed_seconds":round(time.time()-t0,2),"next_cursor":(effective_cursor+len(specs))%total if total else 0,"canonical_mutation":False}
    (out/"fresh_search_summary.json").write_text(json.dumps(summary,indent=2)+"\n",encoding="utf-8"); (out/"provider_events.json").write_text(json.dumps(events,indent=2)+"\n",encoding="utf-8"); print(json.dumps(summary,indent=2))

if __name__=="__main__": main()
