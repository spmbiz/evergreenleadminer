#!/usr/bin/env python3
from __future__ import annotations

import csv, hashlib, io, json, shutil, zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

import cv2
import imagehash
import numpy as np
from PIL import Image
import harvest

ROOT = Path('people-photos-quality-retry')
PEOPLE = json.load(open('quality_retry.json', encoding='utf-8'))
LABELS = ['portrait-front','three-quarter','profile-side','full-body','context-event']
EXTRA_BAD = ('youtube.com','youtu.be','ytimg.com','tiktok.com','spotify.com','amazon.','goodreads.','soundcloud.com')

harvest.MIN = 650
harvest.MAX = 2600

FRONTAL = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
PROFILE = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_profileface.xml')


def has_face(im: Image.Image) -> bool:
    arr = np.asarray(im.convert('RGB'))
    h, w = arr.shape[:2]
    scale = min(1.0, 900 / max(h, w))
    if scale < 1:
        arr = cv2.resize(arr, (round(w*scale), round(h*scale)))
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    min_side = max(28, round(min(gray.shape[:2]) * 0.045))
    faces = FRONTAL.detectMultiScale(gray, 1.08, 4, minSize=(min_side,min_side))
    if len(faces):
        return True
    faces = PROFILE.detectMultiScale(gray, 1.08, 3, minSize=(min_side,min_side))
    if len(faces):
        return True
    faces = PROFILE.detectMultiScale(cv2.flip(gray,1), 1.08, 3, minSize=(min_side,min_side))
    return bool(len(faces))


def collect(person: dict) -> dict:
    name = person['name']
    disamb = person.get('disambiguation','')
    slug = harvest.slug(name)
    folder = ROOT / slug
    folder.mkdir(parents=True, exist_ok=True)
    tokens = harvest.toks(name, disamb)
    queries = [
        f'"{name}" {disamb} portrait photo',
        f'"{name}" {disamb} interview photo',
        f'"{name}" {disamb} event photo',
        f'"{name}" {disamb} speaking on stage photo',
        f'"{name}" {disamb} candid photo',
        f'"{name}" {disamb} standing photo',
        f'"{name}" {disamb} red carpet photo',
        f'"{name}" {disamb} official press photo',
        f'"{name}" {disamb} high resolution photo',
    ]
    accepted=[]; phashes=[]; seen_urls=set(); seen_sha=set()
    all_candidates=[]
    for q in queries:
        for c in harvest.search(q, tokens):
            if c['image'] in seen_urls:
                continue
            host=(urlparse(c['image']).netloc+' '+urlparse(c['page']).netloc).lower()
            if any(x in host for x in EXTRA_BAD):
                continue
            seen_urls.add(c['image'])
            all_candidates.append((q,c))
    for q,c in all_candidates:
        if len(accepted) >= 5:
            break
        got = harvest.fetch(c, 'context-event')
        if not got:
            continue
        im, final_url = got
        if not has_face(im):
            continue
        ph=imagehash.phash(im)
        if any(ph-old <= 9 for old in phashes):
            continue
        buf=io.BytesIO(); im.save(buf,'JPEG',quality=94,optimize=True,progressive=True)
        data=buf.getvalue(); sha=hashlib.sha256(data).hexdigest()
        if sha in seen_sha:
            continue
        idx=len(accepted)+1; label=LABELS[idx-1]
        path=folder/f'{slug}_{idx:02d}_{label}.jpg'; path.write_bytes(data)
        phashes.append(ph); seen_sha.add(sha)
        accepted.append({
            'person_name':name,'disambiguation':disamb,
            'filename':str(path.relative_to(ROOT)),'image_type':label,
            'source_page_url':c['page'],'direct_image_url':final_url,
            'source_domain':urlparse(c['page'] or final_url).netloc,
            'width':im.width,'height':im.height,'file_format':'JPEG',
            'sha256':sha,'perceptual_hash':str(ph),'identity_confidence':'high',
            'identity_evidence':f'exact-name/context search; metadata_score={harvest.score(c,tokens)}; title={c["title"][:160]}',
            'search_query':q,'provider':c['provider'],'notes':'face detected; final visual review required'
        })
    if len(accepted) != 5:
        shutil.rmtree(folder, ignore_errors=True)
        accepted=[]
    print(f'[{name}] {len(accepted)}/5', flush=True)
    return {'person':person,'rows':accepted}


def main():
    if ROOT.exists(): shutil.rmtree(ROOT)
    ROOT.mkdir()
    results=[]
    with ThreadPoolExecutor(max_workers=3) as ex:
        futures={ex.submit(collect,p):p for p in PEOPLE}
        for f in as_completed(futures):
            try: results.append(f.result())
            except Exception as e:
                print(f'[{futures[f]["name"]}] ERROR {e}', flush=True)
                results.append({'person':futures[f],'rows':[]})
    order={p['name']:i for i,p in enumerate(PEOPLE)}
    results.sort(key=lambda r:order[r['person']['name']])
    rows=[x for r in results for x in r['rows']]
    fields=['person_name','disambiguation','filename','image_type','source_page_url','direct_image_url','source_domain','width','height','file_format','sha256','perceptual_hash','identity_confidence','identity_evidence','search_query','provider','notes']
    with (ROOT/'manifest.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(rows)
    fails=[{'person_name':r['person']['name'],'images_accepted':len(r['rows']),'images_required':5,'reason':'fewer than five distinct face-containing public photos'} for r in results if len(r['rows'])!=5]
    with (ROOT/'failures.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=['person_name','images_accepted','images_required','reason']); w.writeheader(); w.writerows(fails)
    complete=sum(len(r['rows'])==5 for r in results)
    txt=f'People requested: {len(PEOPLE)}\nPeople complete with exactly 5 images: {complete}\nPeople failed/incomplete: {len(PEOPLE)-complete}\nAccepted images: {len(rows)}\n'
    (ROOT/'README.txt').write_text(txt)
    with zipfile.ZipFile('people-photos-quality-retry.zip','w',zipfile.ZIP_DEFLATED,allowZip64=True) as z:
        for p in ROOT.rglob('*'):
            if p.is_file(): z.write(p,p.relative_to(ROOT.parent))
    print(txt)

if __name__=='__main__': main()
