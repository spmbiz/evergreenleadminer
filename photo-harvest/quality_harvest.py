#!/usr/bin/env python3
from __future__ import annotations

import csv, hashlib, io, json, shutil, zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

import imagehash
import harvest

ROOT = Path('people-photos-quality-retry')
PEOPLE = json.load(open('quality_retry.json', encoding='utf-8'))
EXTRA_BAD = (
    'youtube.com','youtu.be','ytimg.com','tiktok.com','spotify.com','amazon.',
    'goodreads.','soundcloud.com','pinterest.','pinimg.','facebook.','instagram.'
)
BAD_TEXT = (
    'logo','infographic','quote card','birthday cake','merch','hoodie','t-shirt',
    'album cover','book cover','poster design','wallpaper','vector','illustration',
    'building exterior','headquarters building','storefront only'
)
SLOTS = [
    ('portrait-front', ['portrait headshot photo','close up interview photo','official portrait photo']),
    ('three-quarter', ['three quarter portrait photo','red carpet event photo','award event portrait']),
    ('profile-side', ['side profile photo','speaking side view photo','candid interview photo']),
    ('full-body', ['full body standing photo','full length event photo','walking event photo']),
    ('context-event', ['speaking on stage photo','public appearance photo','professional event photo']),
]

harvest.MIN = 650
harvest.MAX = 2600


def candidate_ok(c: dict) -> bool:
    host = (urlparse(c['image']).netloc + ' ' + urlparse(c['page']).netloc).lower()
    if any(x in host for x in EXTRA_BAD):
        return False
    blob = (c.get('title','') + ' ' + c.get('page','') + ' ' + c.get('image','')).lower()
    if any(x in blob for x in BAD_TEXT):
        return False
    return True


def collect(person: dict) -> dict:
    name = person['name']
    disamb = person.get('disambiguation','')
    slug = harvest.slug(name)
    folder = ROOT / slug
    folder.mkdir(parents=True, exist_ok=True)
    tokens = harvest.toks(name, disamb)
    accepted=[]; phashes=[]; used_urls=set(); seen_sha=set(); failures=[]

    for idx, (label, phrases) in enumerate(SLOTS, start=1):
        found = False
        queries = [f'"{name}" {disamb} {p}' for p in phrases]
        queries += [
            f'"{name}" {disamb} press photo high resolution',
            f'"{name}" {disamb} interview event photo',
            f'"{name}" {disamb} candid professional photo',
        ]
        attempts = 0
        for q in queries:
            for c in harvest.search(q, tokens):
                attempts += 1
                if attempts > 70:
                    break
                if c['image'] in used_urls or not candidate_ok(c):
                    continue
                got = harvest.fetch(c, 'context-event')
                if not got:
                    continue
                im, final_url = got
                ph = imagehash.phash(im)
                if any(ph-old <= 10 for old in phashes):
                    continue
                buf=io.BytesIO(); im.save(buf,'JPEG',quality=94,optimize=True,progressive=True)
                data=buf.getvalue(); sha=hashlib.sha256(data).hexdigest()
                if sha in seen_sha:
                    continue
                path=folder/f'{slug}_{idx:02d}_{label}.jpg'; path.write_bytes(data)
                phashes.append(ph); seen_sha.add(sha); used_urls.add(c['image'])
                accepted.append({
                    'person_name':name,'disambiguation':disamb,
                    'filename':str(path.relative_to(ROOT)),'image_type':label,
                    'source_page_url':c['page'],'direct_image_url':final_url,
                    'source_domain':urlparse(c['page'] or final_url).netloc,
                    'width':im.width,'height':im.height,'file_format':'JPEG',
                    'sha256':sha,'perceptual_hash':str(ph),'identity_confidence':'high',
                    'identity_evidence':f'exact-name/context search; metadata_score={harvest.score(c,tokens)}; title={c["title"][:160]}',
                    'search_query':q,'provider':c['provider'],'notes':'final visual review required'
                })
                found=True
                break
            if found or attempts > 70:
                break
        if not found:
            failures.append(f'{label}: no acceptable distinct candidate after {attempts} results')

    if len(accepted) != 5:
        shutil.rmtree(folder, ignore_errors=True)
        accepted=[]
    print(f'[{name}] {len(accepted)}/5 ' + ('OK' if accepted else 'FAILED | '+'; '.join(failures)), flush=True)
    return {'person':person,'rows':accepted,'failures':failures}


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
                results.append({'person':futures[f],'rows':[],'failures':[str(e)]})
    order={p['name']:i for i,p in enumerate(PEOPLE)}
    results.sort(key=lambda r:order[r['person']['name']])
    rows=[x for r in results for x in r['rows']]
    fields=['person_name','disambiguation','filename','image_type','source_page_url','direct_image_url','source_domain','width','height','file_format','sha256','perceptual_hash','identity_confidence','identity_evidence','search_query','provider','notes']
    with (ROOT/'manifest.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(rows)
    fails=[{'person_name':r['person']['name'],'images_accepted':len(r['rows']),'images_required':5,'reason':' | '.join(r['failures'])} for r in results if len(r['rows'])!=5]
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
