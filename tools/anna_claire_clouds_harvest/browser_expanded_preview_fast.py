#!/usr/bin/env python3
from __future__ import annotations

import csv, hashlib, html, json, os, re, shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from pathlib import Path
from urllib.parse import quote_plus

import imagehash
from PIL import Image, ImageDraw, ImageFont, ImageOps
from playwright.sync_api import sync_playwright

TARGET=int(os.getenv('TARGET_COUNT','300'))
OUT=Path('source_thumb300'); PHOTOS=OUT/'photos'; TMP=Path('source_thumb_tmp')
QUERIES=[
'"Anna Claire Clouds" POV','"Anna Claire Clouds" POV scene','"Anna Claire Clouds" POV preview','"Anna Claire Clouds" POV trailer',
'"Anna Claire Clouds" POVR','"Anna Claire Clouds" "Happy Little Clouds"','"Anna Claire Clouds" "A Girl and Her Canvas"',
'"Anna Claire Clouds" "Intimately POV"','"Anna Claire Clouds" "POV Hookups"','"Anna Claire Clouds" "Mr Big POV"',
'"Anna Claire Clouds" "Manuel\'s Fucking POV 14"','"Anna Claire Clouds" "VR Bangers"','"Anna Claire Clouds" "A Huge Fan"',
'"Anna Claire Clouds" "Double Delight"','"Anna Claire Clouds" VR','"Anna Claire Clouds" VR scene','"Anna Claire Clouds" VR preview',
'"Anna Claire Clouds" VR trailer','"Anna Claire Clouds" scene preview','"Anna Claire Clouds" scene thumbnail',
'"Anna Claire Clouds" video still','"Anna Claire Clouds" trailer','"Anna Claire Clouds" "Dark Side" trailer',
'"Anna Claire Clouds" Cassex trailer','"Anna Claire Clouds" "Blacked Raw" scene','"Anna Claire Clouds" "Going Up" preview',
'"Anna Claire Clouds" "Massive Asses 13"','"Anna Claire Clouds" "Listen to Your Body"','"Anna Claire Clouds" "Dirty Talk" scene',
'"Anna Claire Clouds" "Bound to Please Her"','"Anna Claire Clouds" "Method to Her Badness"','"Anna Claire Clouds" "Karter Kreation"',
'"Anna Claire Clouds" "Lawless" scene','"Anna Claire Clouds" "Fusion" preview','"Anna Claire Clouds" "Can\'t Makeup My Mind"',
'"Ana Clouds" POV'
]
SCENE=['pov','povr','vr','virtual reality','scene','preview','trailer','video still','happy little clouds','a girl and her canvas','intimately pov','pov hookups','mr big pov','manuel','vr bangers','a huge fan','double delight','dark side','cassex','blacked raw','going up','massive asses','listen to your body','dirty talk','bound to please her','method to her badness','karter kreation','lawless','fusion','makeup my mind']
BLOCK=['award','awards','avn expo','xbiz','red carpet','gala','interview','podcast','headshot','portrait','biography','wikipedia','wikimedia','instagram','tiktok','facebook','getty','event photo']


def compact(s): return re.sub(r'[^a-z0-9]','',html.unescape(s).lower())
def relevant(meta):
    text=html.unescape(' '.join(str(meta.get(k) or '') for k in ('t','desc','purl','murl'))).lower(); c=compact(text)
    has_name=any(x in c for x in ('annaclaireclouds','annaclairclouds','anaclouds','annaclouds'))
    return has_name and any(x in text for x in SCENE) and not any(x in text for x in BLOCK)

def high_skin(im):
    small=im.convert('YCbCr').resize((160,100),Image.Resampling.BILINEAR)
    pix=list(small.getdata()); skin=sum(1 for y,cb,cr in pix if 77<=cb<=127 and 133<=cr<=173 and y>45)
    return skin/len(pix)>0.48

def worker(wid, items):
    out=[]; wdir=TMP/f'w{wid}'; wdir.mkdir(parents=True,exist_ok=True)
    with sync_playwright() as p:
        chrome=next((x for x in ('/usr/bin/google-chrome','/usr/bin/google-chrome-stable','/usr/bin/chromium','/usr/bin/chromium-browser') if Path(x).exists()),None)
        b=p.chromium.launch(headless=True,executable_path=chrome,args=['--no-sandbox','--disable-dev-shm-usage','--disable-blink-features=AutomationControlled'])
        c=b.new_context(viewport={'width':1500,'height':930},locale='en-US',user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36')
        c.add_cookies([{'name':'SRCHHPGUSR','value':'ADLT=OFF&NRSLT=50','domain':'.bing.com','path':'/'},{'name':'SRCHUSR','value':'DOB=20200101','domain':'.bing.com','path':'/'}])
        page=c.new_page(); page.set_default_timeout(2500)
        for qi,q in items:
            try: page.goto(f'https://www.bing.com/images/search?q={quote_plus(q)}&form=HDRSC2&adlt=off',wait_until='domcontentloaded',timeout=12000); page.wait_for_timeout(350)
            except: continue
            for _ in range(5): page.mouse.wheel(0,2300); page.wait_for_timeout(100)
            cards=page.locator('a.iusc'); count=min(cards.count(),90); kept=0
            for rank in range(count):
                if kept>=16: break
                card=cards.nth(rank)
                try:
                    meta=json.loads(card.get_attribute('m') or '{}')
                    if not relevant(meta): continue
                    img=card.locator('img.mimg, img').first
                    src=img.evaluate('(i)=>i.currentSrc||i.src||""')
                    if not src: continue
                    r=c.request.get(src,headers={'Referer':'https://www.bing.com/'},timeout=6000)
                    if not r.ok: continue
                    raw=r.body()
                    with Image.open(BytesIO(raw)) as im:
                        im=ImageOps.exif_transpose(im).convert('RGB')
                        if im.width<180 or im.height<110 or im.width/im.height<1.12: continue
                        if high_skin(im): continue
                        fn=f'w{wid}_q{qi:02d}_r{rank+1:03d}_{kept+1:02d}.jpg'
                        im.save(wdir/fn,'JPEG',quality=92,optimize=True,progressive=True)
                    out.append({'tmp_path':str(wdir/fn),'query_index':qi,'query':q,'rank':rank+1,'title':str(meta.get('t') or ''),'source_page':str(meta.get('purl') or ''),'image_url':str(meta.get('murl') or ''),'thumb_url':src})
                    kept+=1
                except: continue
            print(f'worker={wid} q{qi} kept={kept}',flush=True)
        b.close()
    return out

def sheet(rows):
    tw,th,lh,cols,g=190,118,22,8,5; nr=(len(rows)+cols-1)//cols
    can=Image.new('RGB',(g+cols*(tw+g),g+nr*(th+lh+g)),'white'); d=ImageDraw.Draw(can); f=ImageFont.load_default()
    for i,r in enumerate(rows):
        x=g+(i%cols)*(tw+g); y=g+(i//cols)*(th+lh+g)
        try:
            with Image.open(PHOTOS/r['filename']) as im: can.paste(ImageOps.contain(im.convert('RGB'),(tw,th),method=Image.Resampling.LANCZOS),(x,y))
            d.text((x+2,y+th+2),f"#{i+1:03d} q{r['query_index']} r{r['rank']}",fill='black',font=f)
        except: pass
    can.save(OUT/'contact_sheet.jpg','JPEG',quality=88,optimize=True)

def main():
    for p in (OUT,TMP):
        if p.exists(): shutil.rmtree(p)
    PHOTOS.mkdir(parents=True); TMP.mkdir(parents=True)
    chunks=[[] for _ in range(4)]
    for item in enumerate(QUERIES,1): chunks[(item[0]-1)%4].append(item)
    gathered=[]
    with ThreadPoolExecutor(max_workers=4) as ex:
        for f in as_completed([ex.submit(worker,i+1,chunks[i]) for i in range(4)]):
            try:gathered.extend(f.result())
            except Exception as e: print('WORKER_FAIL',e,flush=True)
    rows=[]; exact=set(); phs=[]
    for item in sorted(gathered,key=lambda x:(x['query_index'],x['rank'])):
        if len(rows)>=TARGET: break
        try:
            raw=Path(item['tmp_path']).read_bytes(); sha=hashlib.sha256(raw).hexdigest()
            if sha in exact: continue
            with Image.open(BytesIO(raw)) as im:
                im=ImageOps.exif_transpose(im).convert('RGB'); ph=imagehash.phash(im)
                if any((ph-x)<=2 for x in phs): continue
                idx=len(rows)+1; fn=f'anna_claire_clouds_scene_thumb_{idx:03d}.jpg'
                if im.width<720:
                    s=720/im.width; im=im.resize((720,max(1,int(im.height*s))),Image.Resampling.LANCZOS)
                im.save(PHOTOS/fn,'JPEG',quality=93,optimize=True,progressive=True)
            exact.add(sha); phs.append(ph); item.pop('tmp_path',None); item.update({'index':idx,'filename':fn}); rows.append(item)
        except: continue
    fields=['index','filename','query_index','query','rank','title','source_page','image_url','thumb_url']
    with (OUT/'manifest.csv').open('w',newline='',encoding='utf-8-sig') as f: w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(rows)
    sheet(rows); (OUT/'README.txt').write_text(f'Fresh uncropped Bing source-thumbnail harvest. Collected: {len(rows)}. No prior files reused.\n',encoding='utf-8')
    shutil.make_archive('anna_claire_clouds_uncropped_source_thumbnails','zip',OUT); Path('SOURCE_THUMB_COUNT.txt').write_text(str(len(rows)),encoding='utf-8'); print(f'FINAL_COUNT={len(rows)}',flush=True)
if __name__=='__main__': main()
