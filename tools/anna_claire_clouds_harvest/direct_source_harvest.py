#!/usr/bin/env python3
from __future__ import annotations
import csv, hashlib, json, os, re, shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin, urlparse, quote_plus
import imagehash, requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

TARGET=int(os.getenv('TARGET_COUNT','200')); OUT=Path('direct_output'); PHOTOS=OUT/'photos'
UA='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36'
BLOCK=['pornhub','xvideos','xnxx','xhamster','spankbang','redtube','onlyfans','fansly','manyvids','leak','torrent','nude','naked','hardcore','gangbang','blowjob','anal sex','sex scene','full scene','xxx']
SAFE_HINTS=['award','xma','avn','xbiz','expo','event','interview','podcast','profile','magazine','nom','winner','wins','red carpet','show','stream','panel','signs','joins','guest','featured','feature','honor','honour','celebrates','celebration','appear','attend','photo','gallery','career','director','performer of the year','girl of the month']

def blocked(text):
    s=text.lower(); return any(x in s for x in BLOCK)
def host(u):
    try:return urlparse(u).netloc.lower().removeprefix('www.')
    except:return ''
def get(s,u):
    try:
        r=s.get(u,timeout=18,headers={'User-Agent':UA,'Accept-Language':'en-US,en;q=0.9'});r.raise_for_status();return r
    except:return None

def xbiz_urls(s):
    pages=[f'https://www.xbiz.com/p/anna-claire-clouds?p={i}' for i in range(1,7)]
    articles=set(); direct=[]
    for p in pages:
        r=get(s,p)
        if not r:continue
        soup=BeautifulSoup(r.text,'html.parser')
        for a in soup.select('a[href]'):
            href=urljoin(p,a.get('href','')); title=' '.join(a.get_text(' ',strip=True).split())
            if re.search(r'https://www\.xbiz\.com/(news|features)/\d+/',href):
                articles.add(href.split('?')[0])
                for im in a.select('img'):
                    src=im.get('data-src') or im.get('src') or ''
                    alt=im.get('alt') or title
                    if src:direct.append((urljoin(p,src),href,alt,'XBIZ profile card'))
    return sorted(articles),direct

def parse_article(s,u):
    r=get(s,u)
    if not r:return []
    soup=BeautifulSoup(r.text,'html.parser'); title=(soup.title.get_text(' ',strip=True) if soup.title else '')
    body=' '.join(soup.get_text(' ',strip=True).split())
    if 'anna claire clouds' not in body.lower() and 'anna claire' not in body.lower():return []
    safe=any(h in title.lower() for h in SAFE_HINTS) or any(h in u.lower() for h in SAFE_HINTS)
    if blocked(title) or (not safe and blocked(body[:1800])):return []
    out=[]
    for sel,attr in [('meta[property="og:image"]','content'),('meta[name="twitter:image"]','content')]:
        t=soup.select_one(sel)
        if t and t.get(attr):out.append((urljoin(u,t.get(attr)),u,title,'XBIZ article hero'))
    for im in soup.select('img'):
        src=im.get('data-src') or im.get('data-lazy-src') or im.get('src') or ''
        alt=(im.get('alt') or '')+' '+(im.get('title') or '')
        if src and ('anna claire' in alt.lower() or 'anna-claire' in src.lower() or 'annaclaire' in src.lower()):
            out.append((urljoin(u,src),u,alt or title,'XBIZ named image'))
    return out

def youtube_candidates(s):
    out=[]
    queries=['Anna Claire Clouds interview','Anna Claire Clouds podcast','Anna Claire Clouds awards','Anna Claire Clouds AVN','Anna Claire Clouds XBIZ','Anna Claire Clouds Holly Randall','Anna Claire Clouds Pillow Talk']
    for q in queries:
        r=get(s,'https://www.youtube.com/results?search_query='+quote_plus(q))
        if not r:continue
        txt=r.text
        ids=[]
        for m in re.finditer(r'"videoId":"([A-Za-z0-9_-]{11})"',txt):
            vid=m.group(1)
            if vid not in ids:ids.append(vid)
        for vid in ids[:35]:
            out.append((f'https://i.ytimg.com/vi/{vid}/maxresdefault.jpg',f'https://www.youtube.com/watch?v={vid}',q,'YouTube thumbnail'))
            out.append((f'https://i.ytimg.com/vi/{vid}/hqdefault.jpg',f'https://www.youtube.com/watch?v={vid}',q,'YouTube thumbnail fallback'))
    return out

def getty_candidates(s):
    out=[]
    urls=['https://www.gettyimages.com/photos/anna-claire-clouds','https://www.gettyimages.be/fotos/anna-claire-clouds']
    for u in urls:
        r=get(s,u)
        if not r:continue
        soup=BeautifulSoup(r.text,'html.parser')
        for im in soup.select('img'):
            alt=(im.get('alt') or '')
            src=im.get('src') or im.get('data-src') or ''
            if src and 'anna claire clouds' in alt.lower():out.append((urljoin(u,src),u,alt,'Getty event search'))
            ss=im.get('srcset') or ''
            if 'anna claire clouds' in alt.lower() and ss:
                best=ss.split(',')[-1].strip().split(' ')[0];out.append((urljoin(u,best),u,alt,'Getty event search'))
    return out

def known_candidates(s):
    pages=['https://www.adulttheculture.com/videos/adult-time-vixen-anna-claire-clouds-dishes-on-all-things-adult-culture','https://commons.wikimedia.org/wiki/File:Anna_Claire_Clouds_(2023).png','https://www.themakingofaspieglergirl.com/stars/anna%20claire%20clouds','https://fan.adultentertainmentexpo.com/']
    out=[]
    for u in pages:
        r=get(s,u)
        if not r:continue
        soup=BeautifulSoup(r.text,'html.parser');title=soup.title.get_text(' ',strip=True) if soup.title else u
        for sel,attr in [('meta[property="og:image"]','content'),('meta[name="twitter:image"]','content')]:
            t=soup.select_one(sel)
            if t and t.get(attr):out.append((urljoin(u,t.get(attr)),u,title,'Known interview/event page'))
        for im in soup.select('img'):
            alt=(im.get('alt') or '')
            src=im.get('data-src') or im.get('src') or ''
            if src and ('anna claire' in alt.lower() or 'anna_claire' in src.lower() or 'anna-claire' in src.lower()):out.append((urljoin(u,src),u,alt or title,'Known named image'))
    return out

def fetch_image(item):
    iu,su,title,kind=item
    if blocked(iu+' '+su+' '+title):return None
    try:
        r=requests.get(iu,timeout=18,headers={'User-Agent':UA,'Referer':su,'Accept':'image/avif,image/webp,image/*,*/*;q=0.8'},stream=True)
        r.raise_for_status();ct=(r.headers.get('content-type') or '').lower()
        if 'html' in ct or 'svg' in ct:return None
        b=bytearray()
        for c in r.iter_content(65536):
            b.extend(c)
            if len(b)>20*1024*1024:return None
        with Image.open(BytesIO(b)) as im:
            im.load();im=ImageOps.exif_transpose(im).convert('RGB')
            if min(im.size)<200 or max(im.width/im.height,im.height/im.width)>4:return None
            return im.copy(),bytes(b),item
    except (requests.RequestException,UnidentifiedImageError,OSError,ValueError):return None

def sheet(rows):
    z=140;lab=20;cols=10;gap=5;nr=max(1,(len(rows)+cols-1)//cols)
    c=Image.new('RGB',(gap+cols*(z+gap),gap+nr*(z+lab+gap)),'white');d=ImageDraw.Draw(c);f=ImageFont.load_default()
    for i,r in enumerate(rows):
        x=gap+(i%cols)*(z+gap);y=gap+(i//cols)*(z+lab+gap)
        with Image.open(PHOTOS/r['filename']) as im:c.paste(ImageOps.fit(im.convert('RGB'),(z,z),method=Image.Resampling.LANCZOS),(x,y))
        d.text((x+2,y+z+2),f"{i+1:03d} {r['domain'][:12]}",fill='black',font=f)
    c.save(OUT/'contact_sheet.jpg',quality=88,optimize=True)

def main():
    if OUT.exists():shutil.rmtree(OUT)
    PHOTOS.mkdir(parents=True);s=requests.Session();s.headers.update({'User-Agent':UA})
    articles,direct=xbiz_urls(s);print('XBIZ_ARTICLES',len(articles),flush=True)
    with ThreadPoolExecutor(max_workers=14) as ex:
        fut=[ex.submit(parse_article,s,u) for u in articles]
        for f in as_completed(fut):
            try:direct.extend(f.result())
            except:pass
    direct.extend(youtube_candidates(s));direct.extend(getty_candidates(s));direct.extend(known_candidates(s))
    unique=[];seen=set()
    for x in direct:
        if x[0] not in seen:seen.add(x[0]);unique.append(x)
    print('CANDIDATES',len(unique),flush=True)
    results=[]
    with ThreadPoolExecutor(max_workers=18) as ex:
        fut=[ex.submit(fetch_image,x) for x in unique]
        for f in as_completed(fut):
            r=f.result()
            if r:results.append(r)
    exact=set();ph=[];rows=[]
    for im,raw,item in results:
        if len(rows)>=TARGET:break
        h=hashlib.sha256(raw).hexdigest();p=imagehash.phash(im)
        if h in exact or any(p-o<=2 for o in ph):continue
        exact.add(h);ph.append(p);iu,su,title,kind=item;n=len(rows)+1;fn=f'anna_claire_clouds_{n:03d}.jpg';im.save(PHOTOS/fn,'JPEG',quality=92,optimize=True,progressive=True)
        rows.append({'index':n,'filename':fn,'domain':host(su),'kind':kind,'title':title,'source_page':su,'image_url':iu});print('ACCEPT',n,host(su),kind,title[:80],flush=True)
    with (OUT/'manifest.csv').open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.DictWriter(f,fieldnames=['index','filename','domain','kind','title','source_page','image_url']);w.writeheader();w.writerows(rows)
    sheet(rows);(OUT/'README.txt').write_text(f'Direct-source crawl: XBIZ profile/articles, Getty event search, YouTube interview thumbnails and known public interview/event pages.\nCollected: {len(rows)}\n',encoding='utf-8')
    shutil.make_archive('anna_claire_clouds_direct_source_photos','zip',OUT);print('FINAL_COUNT',len(rows),flush=True)
if __name__=='__main__':main()
