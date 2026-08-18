#!/usr/bin/env python3
from __future__ import annotations
import csv, hashlib, json, os, random, shutil, time
from io import BytesIO
from pathlib import Path
from urllib.parse import quote_plus, urlparse
import imagehash, requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

TARGET=int(os.getenv('TARGET_COUNT','200')); OUT=Path('targeted_output'); PHOTOS=OUT/'photos'
DOMAINS=['xbiz.com','avn.com','gettyimages.com','gettyimages.be','gettyimages.co.uk','youtube.com','youtu.be','ytimg.com','podcasts.apple.com','mzstatic.com','open.spotify.com','scdn.co','adulttheculture.com','adultentertainmentexpo.com','x3mag.com','hollyrandallunfiltered.com','themakingofaspieglergirl.com','wikimedia.org','wikipedia.org','imdb.com','media-amazon.com','news.com.au','shutterstock.com','alamy.com','podchaser.com','listennotes.com','soundcloud.com','iheart.com','podbean.com','buzzsprout.com','libsyn.com']
TERMS=['interview','podcast','awards','red carpet','event','expo','gallery','portrait','AVN Awards','XBIZ Awards','XMA Awards','Adult Entertainment Expo','Holly Randall','Pillow Talk','Adult Time Podcast','2026 event','2025 event','2024 event','2023 event']
QUERIES=[f'"Anna Claire Clouds" {t}' for t in TERMS]+[f'"Anna Claire Clouds" site:{d}' for d in DOMAINS if d not in {'ytimg.com','mzstatic.com','scdn.co','media-amazon.com'}]
BLOCK=['pornhub','xvideos','xnxx','xhamster','spankbang','redtube','onlyfans','fansly','manyvids','leak','torrent','rule34','nude','naked','hardcore','gangbang','blowjob','anal-sex','sex-scene','full-scene','xxx-video']
UA=['Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36','Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/17 Safari/605.1.15']

def host(u):
    try:return urlparse(u).netloc.lower().removeprefix('www.').split(':')[0]
    except:return ''
def okhost(h):return any(h==d or h.endswith('.'+d) for d in DOMAINS)
def blocked(*vals):
    s=' '.join(vals).lower(); return any(x in s for x in BLOCK)

def candidates(s,q):
    seen=set()
    for page in range(10):
        first=page*35+1
        u=f'https://www.bing.com/images/async?q={quote_plus(q)}&first={first}&count=35&adlt=off'
        try:r=s.get(u,timeout=15);r.raise_for_status()
        except Exception as e: print('SEARCHERR',q,page,e,flush=True);continue
        soup=BeautifulSoup(r.text,'html.parser'); found=0; kept=0
        for a in soup.select('a.iusc[m]'):
            try:m=json.loads(a.get('m') or '{}')
            except:continue
            iu=str(m.get('murl') or ''); su=str(m.get('purl') or ''); title=str(m.get('t') or '')
            if not iu.startswith('http') or iu in seen:continue
            seen.add(iu);found+=1
            if not okhost(host(su)):continue
            if blocked(iu,su,title):continue
            kept+=1;yield iu,su,title,q
        print(f'QUERY {q} PAGE {page+1} raw={found} kept={kept}',flush=True)
        if found==0 and page>=2:break
        time.sleep(.15)

def getimg(s,iu,su):
    h={'User-Agent':random.choice(UA),'Accept':'image/avif,image/webp,image/*,*/*;q=0.8'}
    if su.startswith('http'):h['Referer']=su
    try:
        with s.get(iu,headers=h,timeout=14,stream=True,allow_redirects=True) as r:
            r.raise_for_status();ct=(r.headers.get('content-type') or '').lower()
            if 'html' in ct or 'svg' in ct:return None
            b=bytearray()
            for c in r.iter_content(65536):
                b.extend(c)
                if len(b)>18*1024*1024:return None
    except:return None
    try:
        with Image.open(BytesIO(b)) as im:
            im.load();im=ImageOps.exif_transpose(im).convert('RGB')
            if min(im.size)<220 or max(im.width/im.height,im.height/im.width)>4:return None
            return im.copy(),bytes(b)
    except (UnidentifiedImageError,OSError,ValueError):return None

def makesheet(rows):
    z=145; lab=20; cols=10; gap=5; nr=(len(rows)+cols-1)//cols
    c=Image.new('RGB',(gap+cols*(z+gap),gap+max(1,nr)*(z+lab+gap)),'white');d=ImageDraw.Draw(c);f=ImageFont.load_default()
    for i,r in enumerate(rows):
        x=gap+(i%cols)*(z+gap);y=gap+(i//cols)*(z+lab+gap)
        with Image.open(PHOTOS/r['filename']) as im:c.paste(ImageOps.fit(im.convert('RGB'),(z,z),method=Image.Resampling.LANCZOS),(x,y))
        d.text((x+2,y+z+2),f"{i+1:03d} {r['domain'][:13]}",fill='black',font=f)
    c.save(OUT/'contact_sheet.jpg',quality=88,optimize=True)

def main():
    if OUT.exists():shutil.rmtree(OUT)
    PHOTOS.mkdir(parents=True);s=requests.Session();s.headers.update({'User-Agent':random.choice(UA),'Accept-Language':'en-US,en;q=0.9'})
    seen_urls=set();sha=set();ph=[];rows=[]
    for q in QUERIES:
        if len(rows)>=TARGET:break
        for iu,su,title,query in candidates(s,q):
            if len(rows)>=TARGET:break
            if iu in seen_urls:continue
            seen_urls.add(iu);got=getimg(s,iu,su)
            if not got:continue
            im,raw=got;h=hashlib.sha256(raw).hexdigest();p=imagehash.phash(im)
            if h in sha or any(p-o<=2 for o in ph):continue
            n=len(rows)+1;fn=f'anna_claire_clouds_{n:03d}.jpg';im.save(PHOTOS/fn,'JPEG',quality=92,optimize=True,progressive=True)
            sha.add(h);ph.append(p);rows.append({'index':n,'filename':fn,'domain':host(su),'title':title,'source_page':su,'image_url':iu,'query':query});print('ACCEPT',n,host(su),title[:100],flush=True)
    with (OUT/'manifest.csv').open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.DictWriter(f,fieldnames=['index','filename','domain','title','source_page','image_url','query']);w.writeheader();w.writerows(rows)
    makesheet(rows);(OUT/'README.txt').write_text(f'Exact-query, domain-locked public interview/event/editorial harvest.\nCollected: {len(rows)}\n',encoding='utf-8')
    shutil.make_archive('anna_claire_clouds_targeted_public_photos','zip',OUT);print('FINAL_COUNT',len(rows),flush=True)
if __name__=='__main__':main()
