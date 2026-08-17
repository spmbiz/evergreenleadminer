#!/usr/bin/env python3
"""Build strict deterministic TSV staging for MASTER Google Sheet sync.

Commercial Fit V2 is authoritative: only HIGH/MEDIUM A/B sales-ready rows can
enter staging. Historical recall-first C/X/PERMISSIVE rows remain durable
evidence but are quarantined from Enriched Leads sync.
"""
from __future__ import annotations
import argparse,json,re,unicodedata
from pathlib import Path
from urllib.parse import urlparse
from hospitality_quality_v2 import assess_record,sanitize_social
GENERIC_DOMAINS={"google.com","facebook.com","instagram.com","airbnb.com","booking.com","tripadvisor.com","vrbo.com","expedia.com","yelp.com","linktr.ee","wixsite.com","wordpress.com"}
def norm_text(v):
 s=unicodedata.normalize('NFKD',str(v or '')).encode('ascii','ignore').decode().lower();return re.sub(r'[^a-z0-9]+',' ',s).strip()
def norm_phone(v):return re.sub(r'\D+','',str(v or ''))
def norm_domain(v):
 s=str(v or '').strip().lower()
 if not s:return ''
 if '://' not in s:s='http://'+s
 try:h=(urlparse(s).hostname or '').lower().strip('.')
 except:h=''
 if h.startswith('www.'):h=h[4:]
 return '' if h in GENERIC_DOMAINS else h
def record_domain(r):return norm_domain(r.get('domain')) or norm_domain(r.get('website')) or norm_domain(r.get('final_url'))
def source_ref(r):
 if str(r.get('overture_id') or '').strip():return 'overture:'+str(r['overture_id']).strip()
 if str(r.get('source_record_id') or '').strip():return str(r['source_record_id']).strip()
 return 'atp:'+str(r.get('atp_id') or '').strip() if str(r.get('atp_id') or '').strip() else ''
def clean(v):
 if v is None:return ''
 if isinstance(v,(dict,list)):v=json.dumps(v,ensure_ascii=False,separators=(',',':'))
 return str(v).replace('\t',' ').replace('\r',' ').replace('\n',' ')
def strict_quality(r):
 tier=str(r.get('commercial_fit_tier') or '').upper();version=str(r.get('quality_version') or '');sales=str(r.get('sales_ready') or '').upper()
 if version and tier in {'A','B','C','X'}:
  ok=tier in {'A','B'} and sales in {'YES','TRUE','1'} and str(r.get('live_status') or '').upper() in {'HIGH','MEDIUM'}
  return ok,{'commercial_fit_tier':tier,'quality_version':version,'quality_reason':r.get('quality_reason') or 'stored_v2_quality','sanitized_instagram':sanitize_social(r.get('instagram') or '','instagram'),'sanitized_facebook':sanitize_social(r.get('facebook') or '','facebook')}
 q=assess_record(r);ok=q['sales_ready'] and q['commercial_fit_tier'] in {'A','B'} and str(r.get('live_status') or '').upper() in {'HIGH','MEDIUM'};return ok,q
def quality_normalize(r):
 ok,q=strict_quality(r);x=dict(r);x['commercial_fit_tier']=q.get('commercial_fit_tier');x['quality_version']=q.get('quality_version');x['quality_reason']=q.get('quality_reason');x['sales_ready']='YES' if ok else 'NO';x['fit_tier']=q.get('commercial_fit_tier') or x.get('fit_tier');x['instagram']=q.get('sanitized_instagram') or '';x['facebook']=q.get('sanitized_facebook') or '';return ok,x
def richness(r):
 non=sum(bool(str(r.get(k) or '').strip()) for k in ('website','public_email','public_phone','instagram','facebook','whatsapp','contact_page','portfolio_url','operator','final_url','source_url'));fit={'A':2,'B':1}.get(str(r.get('fit_tier') or '').upper(),0);return(non,fit,str(r.get('last_seen') or r.get('_sheet_sync_queued_at') or ''),int(r.get('__seq') or 0))
def primary_key(r):
 d=record_domain(r);p=norm_phone(r.get('public_phone'));n=norm_text(r.get('name'));m=str(r.get('city') or r.get('region') or '').strip();c=str(r.get('country') or '').strip()
 if d:return'domain:'+d
 if len(p)>=7:return'phone:'+p
 if n and m and c:return'name:'+n+'|'+norm_text(m)+'|'+norm_text(c)
 return''
def canonical_row(r):
 fit=str(r.get('fit_tier') or '').upper();score='91' if fit=='A' else '78' if fit=='B' else '';market=str(r.get('city') or r.get('region') or '').strip();country=str(r.get('country') or '').strip();live=str(r.get('live_status') or '').strip().title();enrich='Verified' if live=='High' else 'Qualified' if live=='Medium' else 'Qualification';sources=[]
 for k in ('source_url','instagram_source_url','facebook_source_url','email_source_url','directory_url'):
  v=str(r.get(k) or '').strip()
  if v and v not in sources:sources.append(v)
 note=f"Commercial Fit V2 | fit {fit} | sales-ready | GitHub MASTER dedupe passed"
 return['',fit,score,market,country,str(r.get('name') or '').strip(),'',str(r.get('category') or '').strip(),str(r.get('street') or '').strip(),str(r.get('public_phone') or '').strip(),'','','','','','','',source_ref(r),str(r.get('last_seen') or r.get('first_seen') or '').strip(),'',note,str(r.get('website') or r.get('final_url') or '').strip(),str(r.get('public_email') or '').strip(),str(r.get('contact_page') or '').strip(),str(r.get('instagram') or '').strip(),str(r.get('facebook') or '').strip(),str(r.get('whatsapp') or '').strip(),str(r.get('portfolio_url') or '').strip(),str(r.get('operator') or '').strip(),'','',live,enrich,'; '.join(sources),str(r.get('quality_reason') or r.get('notes') or r.get('live_reason') or '').strip(),'','','']
def load_records(q):
 rows=[];seq=0
 for p in sorted(q.glob('*.jsonl')):
  for line in p.read_text(encoding='utf-8').splitlines():
   try:r=json.loads(line)
   except Exception:continue
   r['__seq']=seq;seq+=1;rows.append(r)
 return rows
def collapse(rows):
 ss=set();sd=set();sp=set();sn=set();kept=[];rej={'source_ref':0,'domain':0,'phone':0,'name_market_country':0}
 for r in sorted(rows,key=richness,reverse=True):
  src=source_ref(r).lower();d=record_domain(r);p=norm_phone(r.get('public_phone'));p=p if len(p)>=7 else '';n=norm_text(r.get('name'));m=norm_text(r.get('city') or r.get('region'));c=norm_text(r.get('country'));nk=(n,m,c) if n and m and c else None;reason=None
  if src and src in ss:reason='source_ref'
  elif d and d in sd:reason='domain'
  elif p and p in sp:reason='phone'
  elif nk and nk in sn:reason='name_market_country'
  if reason:rej[reason]+=1;continue
  kept.append(r)
  if src:ss.add(src)
  if d:sd.add(d)
  if p:sp.add(p)
  if nk:sn.add(nk)
 return kept,rej
def main():
 ap=argparse.ArgumentParser();ap.add_argument('--queue',default='gpt/sheet_sync_queue');ap.add_argument('--out',default='staging/sheet_sync_latest');ap.add_argument('--chunk-rows',type=int,default=500);a=ap.parse_args();q=Path(a.queue);out=Path(a.out);out.mkdir(parents=True,exist_ok=True)
 for p in out.glob('part-*.tsv'):p.unlink()
 raw=load_records(q);qualified=[];quarantine=[]
 for r in raw:
  ok,x=quality_normalize(r);(qualified if ok else quarantine).append(x)
 kept,collapsed=collapse(qualified);parts=[];chunk=max(100,a.chunk_rows)
 for start in range(0,len(kept),chunk):
  batch=kept[start:start+chunk];part=out/f'part-{start//chunk:03d}.tsv'
  with part.open('w',encoding='utf-8',newline='') as f:
   for r in batch:
    market=str(r.get('city') or r.get('region') or '').strip();country=str(r.get('country') or '').strip();phone=norm_phone(r.get('public_phone'));phone=phone if len(phone)>=7 else '';vals=canonical_row(r)+[source_ref(r).lower(),record_domain(r),phone,norm_text(r.get('name')),market,country,str(r.get('live_status') or '').upper(),primary_key(r)];f.write('\t'.join(clean(v) for v in vals)+'\n')
  parts.append({'file':part.name,'rows':len(batch),'start_index':start})
 (out/'quality-quarantine.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in quarantine),encoding='utf-8')
 manifest={'schema_version':2,'quality_gate':'HOSPITALITY_COMMERCIAL_FIT_V2','raw_rows':len(raw),'quality_pass_rows':len(qualified),'quality_quarantined_rows':len(quarantine),'unique_rows':len(kept),'within_queue_collapsed':len(qualified)-len(kept),'collapse_reasons':collapsed,'fit_counts':{t:sum(str(r.get('fit_tier') or '').upper()==t for r in kept) for t in ('A','B')},'live_status_counts':{t:sum(str(r.get('live_status') or '').upper()==t for r in kept) for t in ('HIGH','MEDIUM')},'column_count':46,'parts':parts}
 (out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8');print(json.dumps(manifest,indent=2))
if __name__=='__main__':main()
