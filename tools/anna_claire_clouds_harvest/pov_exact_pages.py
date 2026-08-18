#!/usr/bin/env python3
from __future__ import annotations
import csv, hashlib, html, json, os, random, re, shutil, time
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse
import imagehash, requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

TARGET=int(os.getenv('TARGET_COUNT','200'))
OUT=Path('pov_exact_output'); PHOTOS=OUT/'photos'; TIMEOUT=11; MAX_BYTES=16*1024*1024
QUERIES=[
 '"Anna Claire Clouds" POV',
 '"Anna Claire Clouds" "POV scene"',
 '"Anna Claire Clouds" "POV preview"',
 '"Anna Claire Clouds" "POV trailer"',
 '"Anna Claire Clouds" "video still"',
 '"Anna Claire Clouds" "scene preview"',
 '"Anna Claire Clouds" "Happy Little Clouds"',
 '"Anna Claire Clouds" "Intimately POV"',
 '"Anna Claire Clouds" "Manuel’s Fucking POV 14"',
 '"Anna Claire Clouds" "Manuel\'s Fucking POV 14"',
 '"Anna Claire Clouds" POVR',
 '"Anna Claire Clouds" "VR Bangers"',
 '"Anna Claire Clouds" first person',
]
BLOCK=('gangbang','bukkake','cumshot','blowjob','anal sex','anal-sex','hardcore','full scene','full-scene','leak','torrent','rule34')
SKIP=('google.com','bing.com','microsoft.com','gstatic.com','pinterest.com','artofit.org')
UA='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36'

def host(u):
 try:return urlparse(u).netloc.lower().split(':')[0].removeprefix('www.')
 except:return ''

def name_lock(text):
 t=html.unescape(text or '').lower(); c=re.sub(r'[^a-z]','',t)
 return 'anna claire clouds' in t or 'annaclaireclouds' in c or 'anna-claire-clouds' in t

def blocked(text):return any(x in html.unescape(text or '').lower() for x in BLOCK)

def redirect_url(href):
 if not href:return ''
 if href.startswith('/url?'):
  q=parse_qs(urlparse(href).query); return q.get('q',q.get('url',['']))[0]
 return href if href.startswith('http') else ''

def bing_web_pages(s,q):
 pages=[]
 for first in (1,11,21):
  try:
   r=s.get('https://www.bing.com/search',params={'q':q,'first':first,'count':10,'adlt':'off'},timeout=TIMEOUT); r.raise_for_status()
  except requests.RequestException:continue
  soup=BeautifulSoup(r.text,'html.parser')
  for li in soup.select('li.b_algo'):
   a=li.find('a',href=True)
   if not a:continue
   u=a['href']; title=a.get_text(' ',strip=True); snippet=li.get_text(' ',strip=True)
   h=host(u)
   if not u.startswith('http') or any(h==x or h.endswith('.'+x) for x in SKIP):continue
   if not name_lock(f'{title} {snippet} {u}'):continue
   if blocked(f'{title} {snippet} {u}'):continue
   if u not in pages:pages.append(u)
 return pages

def bing_images(s,q):
 seen=set()
 for page in range(6):
  try:
   u=f'https://www.bing.com/images/async?q={quote_plus(q)}&first={page*35+1}&count=35&adlt=off&qft=%2Bfilterui%3Aphoto-photo'
   r=s.get(u,timeout=TIMEOUT); r.raise_for_status(); soup=BeautifulSoup(r.text,'html.parser')
  except requests.RequestException:continue
  fresh=0
  for a in soup.select('a.iusc[m]'):
   try:m=json.loads(a.get('m') or '{}')
   except:continue
   img=str(m.get('murl') or ''); src=str(m.get('purl') or ''); title=str(m.get('t') or '')
   meta=f'{title} {src} {img}'
   if not img.startswith('http') or img in seen:continue
   seen.add(img)
   if not name_lock(meta) or blocked(meta):continue
   fresh+=1; yield img,src,title,'bing-image-exact'
  print(f'BING IMAGE {q} page {page+1}: {fresh}',flush=True)

def page_images(s,page,q):
 try:
  r=s.get(page,timeout=TIMEOUT,allow_redirects=True); r.raise_for_status()
 except requests.RequestException:return
 if 'text/html' not in (r.headers.get('content-type') or '').lower():return
 soup=BeautifulSoup(r.text,'html.parser'); title=soup.title.get_text(' ',strip=True) if soup.title else ''
 body=soup.get_text(' ',strip=True)[:6000]
 if not name_lock(f'{title} {body} {r.url}'):return
 if blocked(f'{title} {r.url}'):return
 seen=set()
 for sel,attr,origin in [
  ('meta[property="og:image"]','content','page-og-image'),
  ('meta[name="twitter:image"]','content','page-twitter-image'),
  ('meta[name="twitter:image:src"]','content','page-twitter-image'),
  ('video[poster]','poster','video-poster')]:
  for tag in soup.select(sel):
   u=urljoin(r.url,html.unescape(tag.get(attr) or ''))
   if u.startswith('http') and u not in seen:
    seen.add(u); yield u,r.url,title,origin
 for img in soup.find_all('img'):
  u=img.get('data-src') or img.get('data-lazy-src') or img.get('data-original') or img.get('src') or ''
  u=urljoin(r.url,html.unescape(u)); alt=img.get('alt') or ''; it=img.get('title') or ''
  if not u.startswith('http') or u in seen:continue
  clue=f'{alt} {it} {u}'
  if name_lock(clue) or any(k in clue.lower() for k in ('pov','preview','trailer','scene','poster','video')):
   if not blocked(clue):
    seen.add(u); yield u,r.url,f'{title} | {alt}','page-inline'

def fetch(s,u,ref=''):
 try:
  h={'User-Agent':UA,'Accept':'image/avif,image/webp,image/*,*/*;q=0.8'}
  if ref:h['Referer']=ref
  with s.get(u,headers=h,timeout=TIMEOUT,stream=True,allow_redirects=True) as r:
   r.raise_for_status(); ct=(r.headers.get('content-type') or '').lower()
   if 'html' in ct or 'svg' in ct:return None
   b=bytearray()
   for c in r.iter_content(65536):
    if c:b.extend(c)
    if len(b)>MAX_BYTES:return None
  with Image.open(BytesIO(b)) as im:
   im.load(); im=ImageOps.exif_transpose(im).convert('RGB')
   if im.width<200 or im.height<140:return None
   if max(im.width/im.height,im.height/im.width)>4.5:return None
   return im.copy(),bytes(b)
 except (requests.RequestException,UnidentifiedImageError,OSError,ValueError):return None

def sheet(rows):
 size=170; lh=30; cols=8; gap=6; nr=(len(rows)+cols-1)//cols
 c=Image.new('RGB',(gap+cols*(size+gap),gap+nr*(size+lh+gap)),'white'); d=ImageDraw.Draw(c); f=ImageFont.load_default()
 for i,r in enumerate(rows):
  x=gap+(i%cols)*(size+gap); y=gap+(i//cols)*(size+lh+gap)
  try:
   with Image.open(PHOTOS/r['filename']) as im:c.paste(ImageOps.fit(im.convert('RGB'),(size,size),method=Image.Resampling.LANCZOS),(x,y))
  except:continue
  d.text((x+2,y+size+2),f"#{i+1:03d} {r['origin'][:16]}",fill='black',font=f)
  d.text((x+2,y+size+15),r['domain'][:22],fill='black',font=f)
 c.save(OUT/'contact_sheet.jpg',quality=88,optimize=True)

def main():
 if OUT.exists():shutil.rmtree(OUT)
 PHOTOS.mkdir(parents=True); s=requests.Session(); s.headers.update({'User-Agent':UA,'Accept-Language':'en-US,en;q=0.9'})
 rows=[]; seen_urls=set(); hashes=set(); phs=[]; pages=[]
 def accept(u,src,title,origin,q):
  if len(rows)>=TARGET or u in seen_urls:return
  meta=f'{src} {title} {u}'
  if blocked(meta):return
  # For search-engine images, exact-name metadata is mandatory. Page-extracted images inherit an exact-name page.
  if origin=='bing-image-exact' and not name_lock(meta):return
  seen_urls.add(u); got=fetch(s,u,src)
  if not got:return
  im,raw=got; h=hashlib.sha256(raw).hexdigest(); ph=imagehash.phash(im)
  if h in hashes or any(ph-x<=3 for x in phs):return
  i=len(rows)+1; fn=f'anna_claire_clouds_pov_{i:03d}.jpg'; im.save(PHOTOS/fn,'JPEG',quality=92,optimize=True,progressive=True)
  hashes.add(h); phs.append(ph); rows.append({'index':i,'filename':fn,'query':q,'origin':origin,'title':title,'source_page':src,'image_url':u,'domain':host(src) or host(u),'width':im.width,'height':im.height})
  print(f'accepted {i}: {origin} {rows[-1]["domain"]} {title[:70]}',flush=True)
 for q in QUERIES:
  for p in bing_web_pages(s,q):
   if p not in pages:pages.append(p)
  for u,src,title,origin in bing_images(s,q):accept(u,src,title,origin,q)
  if len(rows)>=TARGET:break
 print(f'Exact result pages: {len(pages)}',flush=True)
 for p in pages:
  if len(rows)>=TARGET:break
  for q in QUERIES:
   if name_lock(p) or True:
    for u,src,title,origin in page_images(s,p,q) or []:accept(u,src,title,origin,q)
    break
 fields=['index','filename','query','origin','title','source_page','image_url','domain','width','height']
 with (OUT/'manifest.csv').open('w',newline='',encoding='utf-8-sig') as f:w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
 sheet(rows); (OUT/'README.txt').write_text(f'Exact-name POV/video-preview public thumbnail harvest. Collected {len(rows)} unique images. No generic image fallback was used.\n',encoding='utf-8')
 Path('POV_EXACT_COUNT.txt').write_text(str(len(rows))); shutil.make_archive('anna_claire_clouds_pov_exact','zip',OUT); print(f'FINAL_COUNT={len(rows)}',flush=True)
if __name__=='__main__':main()
