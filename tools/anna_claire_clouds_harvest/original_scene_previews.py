#!/usr/bin/env python3
from __future__ import annotations

import csv, hashlib, html, json, os, random, re, shutil, time
from io import BytesIO
from pathlib import Path
from urllib.parse import quote_plus, urlparse

import imagehash
import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

TARGET = int(os.getenv('TARGET_COUNT', '300'))
OUT = Path('original_scene_previews')
PHOTOS = OUT / 'photos'
TIMEOUT = 12
MAX_BYTES = 15 * 1024 * 1024

QUERIES = [
    '"Anna Claire Clouds" POV preview',
    '"Anna Claire Clouds" POV scene',
    '"Anna Claire Clouds" POV trailer',
    '"Anna Claire Clouds" POVR',
    '"Anna Claire Clouds" "Happy Little Clouds"',
    '"Anna Claire Clouds" "A Girl and Her Canvas"',
    '"Anna Claire Clouds" "Intimately POV"',
    '"Anna Claire Clouds" "POV Hookups"',
    '"Anna Claire Clouds" "Mr Big POV"',
    '"Anna Claire Clouds" "Manuel\'s Fucking POV 14"',
    '"Anna Claire Clouds" "A Huge Fan" VR',
    '"Anna Claire Clouds" "Double Delight" VR',
    '"Anna Claire Clouds" VR preview',
    '"Anna Claire Clouds" VR trailer',
    '"Anna Claire Clouds" virtual reality preview',
    '"Anna Claire Clouds" scene still',
    '"Anna Claire Clouds" scene preview',
    '"Anna Claire Clouds" video still',
    '"Anna Claire Clouds" trailer',
    '"Anna Claire Clouds" movie still',
    '"Anna Claire Clouds" Dark Side trailer',
    '"Anna Claire Clouds" Cassex trailer',
    '"Ana Clouds" POV preview',
    '"Anna Clouds" POV scene',
]

KNOWN = [
    'happy little clouds', 'a girl and her canvas', 'intimately pov', 'pov hookups',
    'mr big pov', 'manuels fucking pov 14', 'a huge fan', 'double delight',
    'dark side', 'cassex', 'povr', 'vr bangers',
]

EDITORIAL_BAD = [
    'interview', 'podcast', 'awards', 'award', 'red carpet', 'gala', 'expo', 'convention',
    'headshot', 'portrait', 'profile photo', 'instagram', 'tiktok', 'twitter', 'x.com',
    'xbiz', 'gettyimages', 'shutterstock', 'alamy', 'imdb name', 'wikidata', 'wikipedia',
]

RANDOM_BAD = [
    'airplane', 'aircraft', 'cloudscape', 'weather', 'cumulus', 'cloud computing',
    'cloud storage', 'sky wallpaper', 'cake', 'dessert', 'kitten', 'cat photo',
]

UA = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/17 Safari/605.1.15',
]

def compact(s: str) -> str:
    return re.sub(r'[^a-z0-9]', '', html.unescape(s).lower())

def host(url: str) -> str:
    try: return urlparse(url).netloc.lower().removeprefix('www.')
    except Exception: return ''

def score(meta: dict, query: str) -> int:
    text = html.unescape(' '.join(str(meta.get(k) or '') for k in ('t','purl','murl','desc'))).lower()
    ct = compact(text)
    s = 0
    if 'annaclaireclouds' in ct or 'annaclairclouds' in ct: s += 12
    elif 'anaclouds' in ct or 'annaclouds' in ct: s += 8
    for title in KNOWN:
        if compact(title) in ct: s += 8
    if 'pov' in text: s += 4
    if 'vr' in text or 'virtual reality' in text: s += 3
    if any(x in text for x in EDITORIAL_BAD): s -= 9
    if any(x in text for x in RANDOM_BAD): s -= 20
    return s

def bing(session: requests.Session, query: str, pages: int = 6):
    for page in range(pages):
        first = page * 35 + 1
        url = f'https://www.bing.com/images/async?q={quote_plus(query)}&first={first}&count=35&adlt=off&qft=%2Bfilterui%3Aphoto-photo'
        try:
            r = session.get(url, timeout=TIMEOUT)
            r.raise_for_status()
        except requests.RequestException:
            continue
        soup = BeautifulSoup(r.text, 'html.parser')
        found = 0
        for rank, tag in enumerate(soup.select('a.iusc[m]'), start=first):
            try: meta = json.loads(tag.get('m') or '{}')
            except Exception: continue
            if score(meta, query) < 11: continue
            found += 1
            yield rank, meta
        print(f'{query} page {page+1}: relevant={found}', flush=True)
        if found == 0 and page >= 2: break
        time.sleep(.15)

def fetch_image(session: requests.Session, image_url: str, source_url: str):
    if not image_url.startswith('http'): return None
    headers = {'User-Agent': random.choice(UA), 'Accept': 'image/avif,image/webp,image/*,*/*;q=0.8'}
    if source_url.startswith('http'): headers['Referer'] = source_url
    try:
        with session.get(image_url, headers=headers, timeout=TIMEOUT, stream=True, allow_redirects=True) as r:
            r.raise_for_status()
            ctype = (r.headers.get('content-type') or '').lower()
            if 'html' in ctype or 'svg' in ctype: return None
            raw = bytearray()
            for chunk in r.iter_content(65536):
                raw.extend(chunk)
                if len(raw) > MAX_BYTES: return None
    except requests.RequestException:
        return None
    try:
        with Image.open(BytesIO(raw)) as im:
            im.load(); im = ImageOps.exif_transpose(im).convert('RGB')
            w, h = im.size
            ratio = w / h
            # Scene/trailer previews should be landscape, not center-cropped faces or gala portraits.
            if w < 420 or h < 220: return None
            if ratio < 1.18 or ratio > 2.6: return None
            return im.copy(), bytes(raw)
    except (UnidentifiedImageError, OSError, ValueError):
        return None

def make_sheet(rows):
    tw, th, lh, cols, gap = 200, 120, 24, 7, 6
    rc = (len(rows)+cols-1)//cols
    canvas = Image.new('RGB', (gap+cols*(tw+gap), gap+rc*(th+lh+gap)), 'white')
    draw, font = ImageDraw.Draw(canvas), ImageFont.load_default()
    for i,row in enumerate(rows):
        x=gap+(i%cols)*(tw+gap); y=gap+(i//cols)*(th+lh+gap)
        try:
            with Image.open(PHOTOS/row['filename']) as im:
                canvas.paste(ImageOps.fit(im.convert('RGB'),(tw,th),method=Image.Resampling.LANCZOS),(x,y))
            draw.text((x+3,y+th+3),f"#{i+1:03d} q{row['query_index']} r{row['rank']}",fill='black',font=font)
        except OSError: pass
    canvas.save(OUT/'contact_sheet.jpg','JPEG',quality=88,optimize=True)

def main():
    if OUT.exists(): shutil.rmtree(OUT)
    PHOTOS.mkdir(parents=True)
    session=requests.Session(); session.headers.update({'User-Agent':random.choice(UA),'Accept-Language':'en-US,en;q=0.9'})
    rows=[]; exact=set(); phashes=[]; seen_urls=set()
    for qi,q in enumerate(QUERIES,1):
        if len(rows)>=TARGET: break
        for rank,meta in bing(session,q):
            if len(rows)>=TARGET: break
            murl=str(meta.get('murl') or '')
            purl=str(meta.get('purl') or '')
            if not murl or murl in seen_urls: continue
            seen_urls.add(murl)
            got=fetch_image(session,murl,purl)
            if not got: continue
            im,raw=got
            sha=hashlib.sha256(raw).hexdigest()
            if sha in exact: continue
            ph=imagehash.phash(im)
            if any((ph-old)<=3 for old in phashes): continue
            idx=len(rows)+1; fn=f'anna_claire_clouds_scene_{idx:03d}.jpg'
            im.save(PHOTOS/fn,'JPEG',quality=93,optimize=True,progressive=True)
            exact.add(sha); phashes.append(ph)
            rows.append({
                'index':idx,'filename':fn,'query_index':qi,'query':q,'rank':rank,
                'title':str(meta.get('t') or ''),'source_page':purl,'image_url':murl,
                'domain':host(purl) or host(murl),'width':im.width,'height':im.height,
            })
            print(f"accepted {idx}: {rows[-1]['domain']} | {rows[-1]['title'][:90]}",flush=True)
    fields=['index','filename','query_index','query','rank','title','source_page','image_url','domain','width','height']
    with (OUT/'manifest.csv').open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(rows)
    make_sheet(rows)
    (OUT/'README.txt').write_text(
        f'Fresh uncropped original-image harvest. Collected: {len(rows)}.\n'
        'Only landscape scene/trailer/POV preview candidates were kept. Editorial, gala, awards and portrait results were rejected.\n'
        'No images from previous packs were reused.\n',encoding='utf-8')
    shutil.make_archive('anna_claire_clouds_uncropped_scene_previews','zip',OUT)
    Path('UNCROPPED_COUNT.txt').write_text(str(len(rows)),encoding='utf-8')
    print(f'FINAL_COUNT={len(rows)}',flush=True)

if __name__=='__main__': main()
