#!/usr/bin/env python3
from __future__ import annotations

import csv, hashlib, io, shutil, zipfile
from pathlib import Path
from urllib.parse import urlparse

import imagehash
import harvest

ROOT = Path('people-photos-hard2-candidates')
PEOPLE = [
    {
        'name':'Expressions Oozing',
        'slug':'expressions-oozing',
        'aliases':['Expressions Oozing','Expressions','Ex Oozing'],
        'context':'British Tottenham Hotspur football YouTuber presenter podcast guest'
    },
    {
        'name':'NileRed',
        'slug':'nilered',
        'aliases':['NileRed','Nigel Braun'],
        'context':'Canadian chemistry YouTuber scientist laboratory creator'
    },
]

harvest.MIN = 500
harvest.MAX = 2600
BAD = ('pinterest.','pinimg.','facebook.','instagram.','gettyimages.','shutterstock.','alamy.','wallpaper')


def identity_ok(c, aliases):
    blob=(c.get('title','')+' '+c.get('page','')+' '+c.get('image','')+' '+c.get('source','')).lower()
    return any(all(part.lower() in blob for part in a.split()) for a in aliases)


def collect(person):
    folder=ROOT/person['slug']; folder.mkdir(parents=True,exist_ok=True)
    aliases=person['aliases']; context=person['context']
    tokens=harvest.toks(person['name'], context+' '+' '.join(aliases))
    queries=[]
    for alias in aliases:
        queries += [
            f'"{alias}" {context} portrait photo',
            f'"{alias}" {context} interview photo',
            f'"{alias}" {context} podcast photo',
            f'"{alias}" {context} event photo',
            f'"{alias}" {context} candid photo',
            f'"{alias}" {context} standing photo',
            f'"{alias}" {context} speaking on stage',
            f'"{alias}" {context} high resolution',
        ]
    accepted=[]; phashes=[]; urls=set(); shas=set(); seen_candidates=set()
    for q in queries:
        if len(accepted)>=12: break
        for c in harvest.search(q,tokens):
            if len(accepted)>=12: break
            key=c['image'].split('?')[0]
            if key in seen_candidates: continue
            seen_candidates.add(key)
            host=(urlparse(c['image']).netloc+' '+urlparse(c['page']).netloc).lower()
            if any(x in host for x in BAD): continue
            if not identity_ok(c,aliases): continue
            got=harvest.fetch(c,'context-event')
            if not got: continue
            im,final=got; ph=imagehash.phash(im)
            if any(ph-old<=8 for old in phashes): continue
            buf=io.BytesIO(); im.save(buf,'JPEG',quality=94,optimize=True,progressive=True)
            data=buf.getvalue(); sha=hashlib.sha256(data).hexdigest()
            if sha in shas: continue
            idx=len(accepted)+1
            path=folder/f'{person["slug"]}_{idx:02d}_candidate.jpg'; path.write_bytes(data)
            phashes.append(ph); urls.add(c['image']); shas.add(sha)
            accepted.append({
                'person_name':person['name'],'filename':str(path.relative_to(ROOT)),
                'source_page_url':c['page'],'direct_image_url':final,
                'source_domain':urlparse(c['page'] or final).netloc,
                'width':im.width,'height':im.height,'sha256':sha,
                'perceptual_hash':str(ph),'search_query':q,'title':c.get('title',''),
                'provider':c.get('provider',''),'notes':'candidate for manual visual selection'
            })
    print(person['name'],len(accepted),flush=True)
    return accepted


def main():
    if ROOT.exists(): shutil.rmtree(ROOT)
    ROOT.mkdir(); rows=[]
    for p in PEOPLE: rows.extend(collect(p))
    fields=['person_name','filename','source_page_url','direct_image_url','source_domain','width','height','sha256','perceptual_hash','search_query','title','provider','notes']
    with (ROOT/'manifest.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(rows)
    (ROOT/'README.txt').write_text(f'People requested: 2\nCandidate images collected: {len(rows)}\n')
    with zipfile.ZipFile('people-photos-hard2-candidates.zip','w',zipfile.ZIP_DEFLATED) as z:
        for p in ROOT.rglob('*'):
            if p.is_file(): z.write(p,p.relative_to(ROOT.parent))

if __name__=='__main__': main()
