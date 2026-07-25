#!/usr/bin/env python3
from pathlib import Path
from urllib.parse import urlparse
import csv, hashlib, io, shutil, zipfile
import imagehash
from PIL import Image, ImageDraw, ImageFont
import harvest_next as h

PERSON={"name":"Chris Evans","disambiguation":"American actor Chris Evans Captain America Marvel","category":"Actors"}
ROOT=Path("chris-evans-candidates")
ZIP=Path("chris-evans-candidates.zip")
BAD=("collage","poster","wallpaper","illustration","meme","magazine cover","book cover","fan art","wax figure","lookalike","group photo")
queries=[
 '"Chris Evans" American actor solo portrait high resolution',
 '"Chris Evans" actor red carpet solo photo',
 '"Chris Evans" actor official portrait',
 '"Chris Evans" actor premiere photo solo',
 '"Chris Evans" actor interview photo',
 '"Chris Evans" actor full body standing',
 '"Chris Evans" Captain America actor press photo',
]
if ROOT.exists(): shutil.rmtree(ROOT)
ROOT.mkdir()
rows=[]; seen=set(); phs=[]
for q in queries:
    for c in h.search(q,PERSON):
        if len(rows)>=12: break
        blob=(c.get('title','')+' '+c.get('page','')+' '+c.get('image','')).lower()
        if any(x in blob for x in BAD): continue
        key=c['image'].split('?')[0]
        if key in seen: continue
        seen.add(key)
        got=h.fetch_image(c,'context-event',PERSON)
        if not got: continue
        im,url,detail=got
        ph=imagehash.phash(im)
        if any(ph-old<=10 for old in phs): continue
        buf=io.BytesIO(); im.save(buf,'JPEG',quality=94,optimize=True,progressive=True)
        data=buf.getvalue(); idx=len(rows)+1
        path=ROOT/f'chris-evans_{idx:02d}_candidate.jpg'; path.write_bytes(data)
        rows.append({
            'person_name':'Chris Evans','candidate_index':idx,'filename':path.name,
            'source_page_url':c.get('page',''),'direct_image_url':url,
            'source_domain':urlparse(c.get('page') or url).netloc,
            'width':im.width,'height':im.height,'sha256':hashlib.sha256(data).hexdigest(),
            'perceptual_hash':str(ph),'title':c.get('title',''),'query':q,'provider':c.get('provider','')
        }); phs.append(ph)
    if len(rows)>=12: break
fields=list(rows[0].keys()) if rows else ['person_name','candidate_index','filename','source_page_url','direct_image_url','source_domain','width','height','sha256','perceptual_hash','title','query','provider']
with (ROOT/'manifest.csv').open('w',encoding='utf-8-sig',newline='') as f:
    w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(rows)
# contact sheet
w0,h0=180,180; cols=6; lines=(len(rows)+cols-1)//cols
can=Image.new('RGB',(cols*w0,max(1,lines)*205),'white'); d=ImageDraw.Draw(can); font=ImageFont.load_default()
for i,r in enumerate(rows):
    im=Image.open(ROOT/r['filename']).convert('RGB'); im.thumbnail((w0-8,h0-8)); x=(i%cols)*w0; y=(i//cols)*205
    tile=Image.new('RGB',(w0,h0),'#ddd'); tile.paste(im,((w0-im.width)//2,(h0-im.height)//2)); can.paste(tile,(x,y)); d.text((x+5,y+184),f'#{i+1}',fill='black',font=font)
can.save(ROOT/'contact_sheet.jpg',quality=90)
(ROOT/'README.txt').write_text(f'Candidates: {len(rows)}\n')
with zipfile.ZipFile(ZIP,'w',zipfile.ZIP_DEFLATED) as z:
    for p in ROOT.rglob('*'):
        if p.is_file(): z.write(p,p.relative_to(ROOT.parent))
print(f'Candidates: {len(rows)}')
