#!/usr/bin/env python3
"""Extract public hospitality contacts from one official Geofabrik OSM PBF.

The Geofabrik JSON index is the source registry. This adapter deliberately uses
small country/subregion extracts, not continent files. It reads only public OSM
tags and emits the same fast/recovery contract as Overture.

No email inference. A candidate needs a published website; website-only rows go
to first-party recovery, website+compatible public email rows go to live verify.
"""
from __future__ import annotations

import argparse,csv,json,re,time
from pathlib import Path
from urllib.parse import urlparse
import requests
import osmium

INDEX='https://download.geofabrik.de/index-v1-nogeom.json'
UA='AIProdLeadHarvester/1.0 (+public-business-research)'
TOURISM={'hotel','resort','guest_house','apartment','chalet','bed_and_breakfast','motel','hostel','camp_site','caravan_site'}
KEEP_TOURISM={'hotel','resort','guest_house','apartment','chalet','bed_and_breakfast'}
FREE_EMAIL={'gmail.com','googlemail.com','outlook.com','hotmail.com','live.com','yahoo.com','icloud.com','me.com','aol.com','proton.me','protonmail.com'}
BAD_EMAIL_DOMAINS={'example.com','example.org','example.net','booking.com','expedia.com','tripadvisor.com','airbnb.com','facebook.com','instagram.com'}
MULTI=('co.uk','org.uk','me.uk','ltd.uk','plc.uk','net.uk','com.au','net.au','org.au','com.br','com.mx','co.nz','net.nz','org.nz','co.za','com.pt','com.es','com.tr','co.jp','com.sg','com.hk','com.my')
COUNTRY_DISPLAY={'MC':'Monaco','BE':'Belgium','FR':'France','ES':'Spain','PT':'Portugal','IT':'Italy','GR':'Greece','GB':'United Kingdom','IE':'Ireland','US':'USA','CA':'Canada','MX':'Mexico','DE':'Germany','AT':'Austria','CH':'Switzerland','NL':'Netherlands','LU':'Luxembourg','HR':'Croatia','MT':'Malta','CY':'Cyprus','ME':'Montenegro','AL':'Albania','SI':'Slovenia','AU':'Australia','NZ':'New Zealand','ZA':'South Africa','MA':'Morocco','AE':'United Arab Emirates'}
FIELDS=['source','source_family','source_release','source_record_id','osm_type','osm_id','country','region','name','category','brand','operator','website','domain','public_email','email_domain','email_domain_match','public_phone','city','state','street','confidence','operator_score','premium_score','fit_tier','source_url','overture_id','notes','instagram','facebook']

def norm(v):return re.sub(r'\s+',' ',str(v or '')).strip()
def root_host(h):
    h=(h or '').lower().strip('.')
    if h.startswith('www.'):h=h[4:]
    if not h:return ''
    for s in MULTI:
        if h==s:return h
        if h.endswith('.'+s):return '.'.join(h.split('.')[-3:])
    p=h.split('.');return '.'.join(p[-2:]) if len(p)>=2 else h
def host(u):
    try:return root_host((urlparse(u).hostname or '').lower())
    except:return ''
def normalize_url(v):
    u=norm(v)
    if not u:return ''
    if not re.match(r'^https?://',u,re.I):
        if re.match(r'^[A-Za-z0-9.-]+\.[A-Za-z]{2,}(/|$)',u):u='https://'+u
        else:return ''
    try:
        p=urlparse(u);return u if p.scheme in ('http','https') and p.hostname else ''
    except:return ''
def email_domain(e):
    e=norm(e).lower().strip('<>[](){}.,;:\"\'');return e.rsplit('@',1)[1] if '@' in e else ''
def valid_email(e):
    e=norm(e).lower().strip('<>[](){}.,;:\"\'')
    if not re.fullmatch(r'[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}',e):return False
    d=email_domain(e)
    return d not in BAD_EMAIL_DOMAINS and not any(d.endswith('.'+x) for x in BAD_EMAIL_DOMAINS) and not any(x in e for x in ('example@','test@','noreply@','no-reply@'))
def resolve_extract(extract_id,iso2):
    r=requests.get(INDEX,timeout=30,headers={'User-Agent':UA});r.raise_for_status();doc=r.json()
    candidates=[]
    for f in doc.get('features') or []:
        p=f.get('properties') or {};url=(p.get('urls') or {}).get('pbf')
        if not url:continue
        ids=p.get('iso3166-1:alpha2') or []
        if extract_id and p.get('id')==extract_id:return p,url
        if iso2 and iso2.upper() in ids:candidates.append((p,url))
    if not candidates:raise RuntimeError(f'Geofabrik extract not found id={extract_id} iso2={iso2}')
    # Prefer smallest/specific id depth rather than a continent containing country.
    candidates.sort(key=lambda x:(-str(x[0].get('id') or '').count('/'),len(str(x[0].get('id') or ''))))
    return candidates[0]
def download(url,path,max_bytes):
    with requests.get(url,stream=True,timeout=60,headers={'User-Agent':UA}) as r:
        r.raise_for_status();cl=int(r.headers.get('Content-Length') or 0)
        if cl and cl>max_bytes:raise RuntimeError(f'extract too large: {cl} > {max_bytes}')
        total=0
        with open(path,'wb') as f:
            for c in r.iter_content(1024*1024):
                if not c:continue
                total+=len(c)
                if total>max_bytes:raise RuntimeError(f'extract exceeded cap: {total} > {max_bytes}')
                f.write(c)
    return total
class Handler(osmium.SimpleHandler):
    def __init__(self,country,region):super().__init__();self.country=country;self.region=region;self.rows=[];self.seen=set();self.rejects={}
    def node(self,o):self._obj(o,'node')
    def way(self,o):self._obj(o,'way')
    def relation(self,o):self._obj(o,'relation')
    def _obj(self,o,kind):
        t={x.k:x.v for x in o.tags};tour=norm(t.get('tourism')).lower()
        if tour not in KEEP_TOURISM:return
        name=norm(t.get('name') or t.get('brand') or t.get('operator'))
        website=normalize_url(t.get('contact:website') or t.get('website') or t.get('url'))
        if not name or not website:return
        domain=host(website)
        if not domain or domain in self.seen:return
        email=norm(t.get('contact:email') or t.get('email')).lower()
        if email and not valid_email(email):email=''
        eroot=root_host(email_domain(email)) if email else ''
        if email and eroot!=domain and eroot not in FREE_EMAIL:email='';eroot=''
        phone=norm(t.get('contact:phone') or t.get('phone'))
        city=norm(t.get('addr:city'));state=norm(t.get('addr:state'));street=' '.join(x for x in (norm(t.get('addr:housenumber')),norm(t.get('addr:street'))) if x)
        brand=norm(t.get('brand'));operator=norm(t.get('operator'))
        premium=80 if tour=='resort' else 70 if tour in ('hotel','chalet') else 58
        op_score=65 if operator else 45
        oid=str(o.id);source_url=f'https://www.openstreetmap.org/{kind}/{oid}'
        row={'source':'OpenStreetMap via Geofabrik','source_family':'openstreetmap_geofabrik','source_release':self.region,'source_record_id':f'osm:{kind}:{oid}','osm_type':kind,'osm_id':oid,'country':self.country,'region':self.region,'name':name,'category':tour,'brand':brand,'operator':operator,'website':website,'domain':domain,'public_email':email,'email_domain':email_domain(email),'email_domain_match':'YES' if email and eroot==domain else ('FREE_WEBMAIL' if email else ''),'public_phone':phone,'city':city,'state':state,'street':street,'confidence':'OSM_PUBLIC_TAGS','operator_score':str(op_score),'premium_score':str(premium),'fit_tier':'A' if premium>=70 or op_score>=65 else 'B','source_url':source_url,'overture_id':'','notes':'Public OSM tourism/contact tags via official Geofabrik extract; no inference.','instagram':normalize_url(t.get('contact:instagram')),'facebook':normalize_url(t.get('contact:facebook'))}
        self.seen.add(domain);self.rows.append(row)
def write_csv(path,rows):
    with open(path,'w',encoding='utf-8',newline='') as f:w=csv.DictWriter(f,fieldnames=FIELDS);w.writeheader();w.writerows(rows)
def main():
    ap=argparse.ArgumentParser();ap.add_argument('--extract-id',default='');ap.add_argument('--iso2',default='');ap.add_argument('--country',default='');ap.add_argument('--outdir',required=True);ap.add_argument('--max-download-mb',type=int,default=800);a=ap.parse_args();t0=time.time()
    props,url=resolve_extract(a.extract_id,a.iso2);out=Path(a.outdir);out.mkdir(parents=True,exist_ok=True);pbf=out/'source.osm.pbf';size=download(url,pbf,a.max_download_mb*1024*1024)
    iso=(a.iso2 or ((props.get('iso3166-1:alpha2') or [''])[0])).upper();country=a.country or COUNTRY_DISPLAY.get(iso,iso);region=str(props.get('id') or a.extract_id or iso)
    h=Handler(country,region);h.apply_file(str(pbf),locations=False);pbf.unlink(missing_ok=True)
    fast=[r for r in h.rows if r['public_email']];recovery=[r for r in h.rows if not r['public_email']]
    write_csv(out/'v6_fast_ready.csv',fast);write_csv(out/'v6_recovery_candidates.csv',recovery)
    summary={'extract_id':region,'iso2':iso,'country':country,'pbf_url':url,'download_bytes':size,'hospitality_domains':len(h.rows),'fast_ready':len(fast),'recovery_candidates':len(recovery),'elapsed_seconds':round(time.time()-t0,2)}
    (out/'osm_summary.json').write_text(json.dumps(summary,indent=2)+'\n');print(json.dumps(summary,indent=2))
if __name__=='__main__':main()
