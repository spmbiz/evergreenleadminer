#!/usr/bin/env python3
from __future__ import annotations
import csv, hashlib, html, json, os, random, re, shutil
from io import BytesIO
from pathlib import Path
from urllib.parse import quote_plus, urlparse
import imagehash, requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

TARGET=int(os.getenv('TARGET_COUNT','200'))
OUT=Path('pov_fast_output'); PHOTOS=OUT/'photos'; TIMEOUT=9; MAX_BYTES=14*1024*1024
QUERIES=[
 '"Anna Claire Clouds" POV', '"Anna Claire Clouds" POV scene',
 '"Anna Claire Clouds" POV video', '"Anna Claire Clouds" POV preview',
 '"Anna Claire Clouds" POV trailer', '"Anna Claire Clouds" video still',
 '"Anna Claire Clouds" scene preview', '"Anna Claire Clouds" trailer screenshot',
 '"Anna Claire Clouds" first person scene', '"Anna Claire Clouds" VR POV',
 '"Anna Claire Clouds" POVR', '"Anna Claire Clouds" "Happy Little Clouds"',
]
BLOCK=('gangbang','bukkake','cumshot','blowjob','anal sex','anal-sex','hardcore','full scene','full-scene','leak','torrent','rule34')
UA='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36'

def host(u):
 try:return urlparse(u).netloc.lower().removeprefix('www.')
 except:return ''

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
   if im.width<180 or im.height<140:return None
   if max(im.width/im.height,im.height/im.width)>4.5:return None
   return im.copy(),bytes(b)
 except (requests.RequestException,UnidentifiedImageError,OSError,ValueError):return None

def candidates(s,q):
 seen=set()
 # Google Images HTML thumbnails + embedded original URLs.
 try:
  r=s.get('https://www.google.com/search',params={'tbm':'isch','q':q,'safe':'off','hl':'en'},timeout=TIMEOUT); r.raise_for_status()
  soup=BeautifulSoup(r.text,'html.parser')
  for im in soup.find_all('img'):
   u=html.unescape(im.get('data-src') or im.get('src') or '').replace('\\u003d','=').replace('\\u0026','&').replace('\\/','/')
   if u.startswith('http') and u not in seen:
    seen.add(u); yield u,'',im.get('alt') or '','google-thumbnail'
  for pat in (r'"ou":"(https?://[^" ]+)"',r'(https?://[^"\\ ]+\.(?:jpg|jpeg|png|webp)(?:\?[^"\\ ]*)?)'):
   for raw in re.findall(pat,r.text,re.I):
    u=html.unescape(raw).replace('\\u003d','=').replace('\\u0026','&').replace('\\/','/')
    if u.startswith('http') and u not in seen:
     seen.add(u); yield u,'','','google-original'
 except requests.RequestException:pass
 # Exact query Bing Images fallback, no broad terms.
 for page in range(3):
  try:
   url=f'https://www.bing.com/images/async?q={quote_plus(q)}&first={page*35+1}&count=35&adlt=off&qft=%2Bfilterui%3Aphoto-photo'
   r=s.get(url,timeout=TIMEOUT); r.raise_for_status(); soup=BeautifulSoup(r.text,'html.parser')
   for a in soup.select('a.iusc[m]'):
    try:m=json.loads(a.get('m') or '{}')
    except:continue
    u=str(m.get('murl') or ''); p=str(m.get('purl') or ''); t=str(m.get('t') or '')
    if u.startswith('http') and u not in seen:
     seen.add(u); yield u,p,t,'bing-exact-pov'
  except requests.RequestException:continue

def sheet(rows):
 size=160; label=27; cols=8; gap=6; nr=(len(rows)+cols-1)//cols
 c=Image.new('RGB',(gap+cols*(size+gap),gap+nr*(size+label+gap)),'white'); d=ImageDraw.Draw(c); f=ImageFont.load_default()
 for i,r in enumerate(rows):
  x=gap+(i%cols)*(size+gap); y=gap+(i//cols)*(size+label+gap)
  try:
   with Image.open(PHOTOS/r['filename']) as im:c.paste(ImageOps.fit(im.convert('RGB'),(size,size),method=Image.Resampling.LANCZOS),(x,y))
  except:continue
  d.text((x+2,y+size+2),f"#{i+1:03d} {r['query_short'][:22]}",fill='black',font=f)
 c.save(OUT/'contact_sheet.jpg',quality=88,optimize=True)

def main():
 if OUT.exists():shutil.rmtree(OUT)
 PHOTOS.mkdir(parents=True); s=requests.Session(); s.headers.update({'User-Agent':UA,'Accept-Language':'en-US,en;q=0.9'})
 rows=[]; seen_urls=set(); hashes=set(); phs=[]
 for q in QUERIES:
  if len(rows)>=TARGET:break
  for u,p,t,o in candidates(s,q):
   if len(rows)>=TARGET:break
   text=f'{u} {p} {t}'.lower()
   if any(x in text for x in BLOCK) or u in seen_urls:continue
   seen_urls.add(u); got=fetch(s,u,p)
   if not got:continue
   im,raw=got; h=hashlib.sha256(raw).hexdigest(); ph=imagehash.phash(im)
   if h in hashes or any(ph-x<=3 for x in phs):continue
   i=len(rows)+1; fn=f'anna_claire_clouds_pov_{i:03d}.jpg'; im.save(PHOTOS/fn,'JPEG',quality=92,optimize=True,progressive=True)
   hashes.add(h); phs.append(ph); rows.append({'index':i,'filename':fn,'query':q,'query_short':q.replace('"Anna Claire Clouds" ','').replace('"',''),'origin':o,'title':t,'source_page':p,'image_url':u,'domain':host(p) or host(u),'width':im.width,'height':im.height})
   print(f'accepted {i}: {o} {rows[-1]["domain"]} {q}',flush=True)
 fields=['index','filename','query','query_short','origin','title','source_page','image_url','domain','width','height']
 with (OUT/'manifest.csv').open('w',newline='',encoding='utf-8-sig') as f:w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
 sheet(rows); (OUT/'README.txt').write_text(f'Fast exact-query POV/video-still harvest. Collected {len(rows)} unique public thumbnails/stills.\n',encoding='utf-8')
 Path('POV_FAST_COUNT.txt').write_text(str(len(rows))); shutil.make_archive('anna_claire_clouds_pov_fast','zip',OUT); print(f'FINAL_COUNT={len(rows)}',flush=True)
if __name__=='__main__':main()
