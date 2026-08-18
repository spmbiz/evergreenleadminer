#!/usr/bin/env python3
from __future__ import annotations

import csv, hashlib, io, shutil, zipfile
from pathlib import Path
from urllib.parse import urlparse

import imagehash
import harvest_next as h

OUT = Path('people-photos-next93-retry4')
ZIP = Path('people-photos-next93-retry4.zip')
PEOPLE = [
    {'name':'Snoop Dogg','aliases':['Snoop Dogg','Calvin Broadus'],'context':'American rapper actor entrepreneur'},
    {'name':'T.I.','aliases':['T.I. rapper','TI rapper','Tip Harris','Clifford Harris'],'context':'American rapper actor Atlanta'},
    {'name':'Logic','aliases':['Logic rapper','Bobby Hall rapper','Sir Robert Bryson Hall'],'context':'American rapper musician'},
    {'name':'Rick Ross','aliases':['Rick Ross rapper','William Leonard Roberts rapper'],'context':'American rapper Maybach Music Group'},
]
LABELS = ['portrait-front','three-quarter','profile-side','full-body','context-event']
PHRASES = [
    'portrait headshot high resolution',
    'press portrait event',
    'side profile interview',
    'full body standing red carpet',
    'performing on stage public appearance',
    'award show photo',
    'candid interview photo',
    'professional press photo',
]

h.MIN_LONG = 500
h.MAX_LONG = 2600


def blob(c):
    return h.norm(' '.join([c.get('title',''),c.get('page',''),c.get('image',''),c.get('source',''),c.get('description','')]))


def alias_score(c, aliases):
    b=blob(c); score=0
    for alias in aliases:
        a=h.norm(alias)
        if a and a in b: score=max(score,12)
        toks=[x for x in a.split() if len(x)>=2]
        score=max(score, sum(t in b for t in toks)*3)
    if any(x in (c.get('page','')+c.get('image','')).lower() for x in h.GOOD_DOMAINS): score+=2
    if any(x in b for x in h.BAD_TEXT): score-=10
    return score


def raw_search(query, aliases):
    rows=h.commons_search(query)+h.ddg_search(query)
    if len(rows)<40: rows+=h.bing_search(query)
    out=[]; seen=set()
    for c in rows:
        key=c.get('image','').split('?')[0]
        if not key or key in seen or h.is_bad_url(c.get('image','')) or h.is_bad_url(c.get('page','')): continue
        seen.add(key)
        s=alias_score(c,aliases)
        if s<3: continue
        out.append((s,0 if c.get('provider')=='commons' else 1,c))
    out.sort(key=lambda x:(-x[0],x[1]))
    return [x[2] for x in out]


def fetch(c, label, person):
    # Use the existing robust downloader and image checks, with a query-safe identity person.
    proxy={'name':person['aliases'][0], 'disambiguation':person['context']}
    return h.fetch_image(c,label,proxy)


def harvest_person(person):
    slug=h.slug(person['name']); folder=OUT/slug; folder.mkdir(parents=True,exist_ok=True)
    rows=[]; phashes=[]; shas=set(); urls=set(); candidates=[]; seen=set()
    for alias in person['aliases']:
        for phrase in PHRASES:
            q=f'"{alias}" {person["context"]} {phrase}'
            for c in raw_search(q,person['aliases']):
                key=c['image'].split('?')[0]
                if key in seen: continue
                seen.add(key); c['query']=q; candidates.append(c)
    for c in candidates:
        if len(rows)>=5: break
        if c['image'] in urls: continue
        label=LABELS[len(rows)]
        got=fetch(c,'context-event',person)
        if not got: continue
        im,final,face=got
        ph=imagehash.phash(im)
        if any(ph-old<=8 for old in phashes): continue
        buf=io.BytesIO(); im.save(buf,'JPEG',quality=94,optimize=True,progressive=True)
        data=buf.getvalue(); sha=hashlib.sha256(data).hexdigest()
        if sha in shas: continue
        idx=len(rows)+1; path=folder/f'{slug}_{idx:02d}_{label}.jpg'; path.write_bytes(data)
        phashes.append(ph); shas.add(sha); urls.add(c['image'])
        rows.append({
            'person_name':person['name'],'disambiguation':person['context'],
            'filename':str(path.relative_to(OUT)),'image_type':label,
            'source_page_url':c.get('page',''),'direct_image_url':final,
            'source_domain':urlparse(c.get('page') or final).netloc,
            'width':im.width,'height':im.height,'file_format':'JPEG',
            'sha256':sha,'perceptual_hash':str(ph),'identity_confidence':'high',
            'identity_evidence':f'alias/context search; alias_score={alias_score(c,person["aliases"])}; title={c.get("title","")[:180]}',
            'search_query':c.get('query',''),'provider':c.get('provider',''),'notes':'targeted fallback; visual review required'
        })
    if len(rows)!=5:
        shutil.rmtree(folder,ignore_errors=True); rows=[]
    print(f'[{person["name"]}] {len(rows)}/5',flush=True)
    return rows


def main():
    if OUT.exists(): shutil.rmtree(OUT)
    OUT.mkdir(); all_rows=[]; failures=[]
    for p in PEOPLE:
        rows=harvest_person(p); all_rows.extend(rows)
        if len(rows)!=5: failures.append({'person_name':p['name'],'images_accepted':0,'images_required':5,'reason':'fewer than five distinct alias-verified candidates'})
    fields=['person_name','disambiguation','filename','image_type','source_page_url','direct_image_url','source_domain','width','height','file_format','sha256','perceptual_hash','identity_confidence','identity_evidence','search_query','provider','notes']
    with (OUT/'manifest.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(all_rows)
    with (OUT/'failures.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=['person_name','images_accepted','images_required','reason']);w.writeheader();w.writerows(failures)
    complete=len(all_rows)//5
    (OUT/'README.txt').write_text(f'People requested: 4\nPeople complete with exactly 5 images: {complete}\nPeople failed/incomplete: {4-complete}\nAccepted images: {len(all_rows)}\n')
    with zipfile.ZipFile(ZIP,'w',zipfile.ZIP_DEFLATED,allowZip64=True) as z:
        for p in OUT.rglob('*'):
            if p.is_file(): z.write(p,p.relative_to(OUT.parent))

if __name__=='__main__': main()
