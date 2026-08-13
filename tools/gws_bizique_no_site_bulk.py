#!/usr/bin/env python3
from __future__ import annotations
import csv,json,re,time,random,unicodedata
from concurrent.futures import ThreadPoolExecutor,as_completed
from pathlib import Path
from urllib.parse import urlparse,parse_qs,unquote
import requests
from bs4 import BeautifulSoup

OUT=Path('results/gws_bizique_no_site_bulk'); OUT.mkdir(parents=True,exist_ok=True)
UA='Mozilla/5.0 (compatible; GWS-Brussels-BulkVerifier/2.0; public-business-research)'
MUNICIPALITIES=[
'Bruxelles','Laeken','Neder-Over-Heembeek','Haren','Schaerbeek','Evere','Etterbeek','Ixelles','Saint-Gilles','Anderlecht','Molenbeek-Saint-Jean','Koekelberg','Berchem-Sainte-Agathe','Ganshoren','Jette','Uccle','Forest','Auderghem','Watermael-Boitsfort','Woluwe-Saint-Lambert','Woluwe-Saint-Pierre','Saint-Josse-ten-Noode'
]
VERTICALS=['coiffure','salon de coiffure','barber','institut de beauté','beauty salon','nails','manucure','garage','réparation auto','boucherie','boulangerie','laundry','laverie','téléphone','gsm','toiletteur','cordonnier','fleuriste']
NO_SITE_PHRASES=['il n existe aucun site web repertorie','aucun site web repertorie','geen website vermeld','geen website geregistreerd']
TARGET_HINTS=['hair','coiff','barber','beaut','nail','manuc','pedic','garage','auto','butcher','boucher','bakery','boulanger','laundry','laver','gsm','teleph','mobile','groom','toilett','cordonn','fleur']
BLOCKED=['facebook.com','instagram.com','tiktok.com','fresha.com','planity.com','treatwell.','salonkee.','pagesdor.','goudengids.','bizique.be','cylex-','opendi.','heures.be','hours.be','openingsuren.','ivof.','selfcity.','waze.','google.','maps.apple.']

def norm(s):
 s=unicodedata.normalize('NFKD',str(s or '')).encode('ascii','ignore').decode().lower()
 s=re.sub(r'[^a-z0-9]+',' ',s); return re.sub(r'\s+',' ',s).strip()
def digits(s): return re.sub(r'\D','',str(s or ''))
def host(u):
 try:
  h=(urlparse(u).hostname or '').lower().strip('.')
  return h[4:] if h.startswith('www.') else h
 except:return ''
def decode_bing(u):
 if 'bing.com/ck/a' not in u:return u
 try:
  q=parse_qs(urlparse(u).query); x=q.get('u',[''])[0]
  if x.startswith('a1'):
   import base64
   s=x[2:]; s += '='*((4-len(s)%4)%4)
   return base64.urlsafe_b64decode(s).decode('utf-8','ignore')
 except:pass
 return u

def bing(q,session):
 r=session.get('https://www.bing.com/search',params={'q':q,'count':50,'setlang':'fr-BE'},headers={'User-Agent':UA},timeout=15)
 r.raise_for_status(); s=BeautifulSoup(r.text,'html.parser'); out=[]
 for a in s.select('li.b_algo h2 a'):
  u=decode_bing(a.get('href',''))
  if u.startswith('https://www.bizique.be/') and '/nl/' not in u and u.count('/')>=3: out.append(u.split('?')[0])
 return out

def search_urls():
 sess=requests.Session(); urls=set(); stats=[]
 queries=[]
 for m in MUNICIPALITIES:
  for v in VERTICALS:
   queries.append(f'site:bizique.be "{m}" "{v}" "Il n\'existe aucun site Web répertorié"')
 # broaden with phrase-only municipality queries
 for m in MUNICIPALITIES:
  queries.append(f'site:bizique.be "{m}" "aucun site Web répertorié"')
 for i,q in enumerate(queries,1):
  try:
   found=bing(q,sess); urls.update(found); stats.append({'query':q,'results':len(found),'ok':1})
  except Exception as e: stats.append({'query':q,'results':0,'ok':0,'error':type(e).__name__})
  if i%20==0: print('search',i,'/',len(queries),'urls',len(urls),flush=True)
  time.sleep(0.18+random.random()*0.18)
 return sorted(urls),stats

def page(u):
 s=requests.Session(); r=s.get(u,headers={'User-Agent':UA},timeout=12); r.raise_for_status()
 soup=BeautifulSoup(r.text,'html.parser'); text=' '.join(soup.stripped_strings); nt=norm(text)
 if not any(p in nt for p in NO_SITE_PHRASES): return None
 title=(soup.find('h1').get_text(' ',strip=True) if soup.find('h1') else (soup.title.get_text(' ',strip=True) if soup.title else ''))
 # Extract common structured labels from text
 phones=re.findall(r'(?:\+32|0)\s*\d(?:[\s./-]*\d){7,9}',text)
 emails=re.findall(r'[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}',text,re.I)
 postal=re.findall(r'\b(1\d{3})\b',text)
 # only Brussels-capital postcodes roughly 1000-1210; reject obvious outside region
 pc=''
 for x in postal:
  n=int(x)
  if 1000<=n<=1210: pc=x; break
 if not pc:return None
 # address: first text fragment around postcode
 addr=''
 m=re.search(r'([^\n]{5,120}\b'+re.escape(pc)+r'\b[^\n]{0,80})',soup.get_text('\n',strip=True))
 if m: addr=re.sub(r'\s+',' ',m.group(1)).strip()
 # category-ish relevance gate from full text/title
 if not any(h in nt for h in TARGET_HINTS): return None
 # historical dead site lines
 historical=[]
 hist=False
 for st in soup.stripped_strings:
  ns=norm(st)
  if 'informations historiques' in ns or 'historische informatie' in ns: hist=True; continue
  if hist and re.search(r'\b(?:www\.)?[a-z0-9.-]+\.(?:be|com|net|org|eu)\b',st,re.I): historical.append(st.strip())
 # collect external hrefs, classify any non-platform as suspicious rather than auto-owned
 external=[]
 for a in soup.find_all('a',href=True):
  href=a['href']; h=host(href)
  if h and h!='bizique.be' and not h.endswith('.bizique.be'): external.append(href)
 external=list(dict.fromkeys(external))
 suspicious=[x for x in external if not any(b in host(x) for b in BLOCKED)]
 return {'name':title,'address':addr,'postalcode':pc,'phone':phones[0] if phones else '', 'normalized_phone':digits(phones[0]) if phones else '', 'email':emails[0] if emails else '', 'bizique_url':u, 'explicit_no_site':True, 'historical_domains':' | '.join(historical[:8]), 'external_urls':' | '.join(external[:12]), 'suspicious_external_urls':' | '.join(suspicious[:8])}

def main():
 t=time.time(); urls,stats=search_urls(); print('candidate urls',len(urls),flush=True)
 rows=[]
 with ThreadPoolExecutor(max_workers=8) as ex:
  fs={ex.submit(page,u):u for u in urls}
  for i,f in enumerate(as_completed(fs),1):
   try:
    r=f.result();
    if r: rows.append(r)
   except Exception: pass
   if i%50==0: print('fetch',i,'/',len(urls),'qualified',len(rows),flush=True)
 # dedupe exact phone else name+postal
 seen=set(); uniq=[]
 for r in rows:
  k=('p',r['normalized_phone']) if r['normalized_phone'] else ('n',norm(r['name']),r['postalcode'])
  if k in seen:continue
  seen.add(k);uniq.append(r)
 uniq.sort(key=lambda r:(0 if r['suspicious_external_urls'] else 1, bool(r['normalized_phone']), bool(r['email'])),reverse=True)
 fields=list(uniq[0].keys()) if uniq else ['name']
 with (OUT/'bizique_explicit_no_site.csv').open('w',encoding='utf-8',newline='') as f:
  w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(uniq)
 with (OUT/'search_queries.csv').open('w',encoding='utf-8',newline='') as f:
  ff=['query','results','ok','error']; w=csv.DictWriter(f,fieldnames=ff,extrasaction='ignore');w.writeheader();w.writerows(stats)
 summary={'queries':len(stats),'queries_ok':sum(x.get('ok',0) for x in stats),'bizique_urls':len(urls),'explicit_no_site_rows':len(rows),'unique_explicit_no_site':len(uniq),'with_phone':sum(bool(r['normalized_phone']) for r in uniq),'with_email':sum(bool(r['email']) for r in uniq),'suspicious_external_present':sum(bool(r['suspicious_external_urls']) for r in uniq),'elapsed_seconds':round(time.time()-t,2)}
 (OUT/'summary.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False),encoding='utf-8')
 print('GWS_BIZIQUE_BULK_SUMMARY='+json.dumps(summary,ensure_ascii=False),flush=True)
if __name__=='__main__':main()
