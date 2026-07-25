#!/usr/bin/env python3
from pathlib import Path
from urllib.parse import urlparse
import csv, hashlib, io, shutil, zipfile
import imagehash
import harvest

NAME = 'Caleb Pressley'
DISAMB = 'American interviewer Sundae Conversation Barstool Sports former North Carolina football quarterback'
ROOT = Path('people-photos-caleb')
SLUG = 'caleb-pressley'
FOLDER = ROOT / SLUG

if ROOT.exists():
    shutil.rmtree(ROOT)
FOLDER.mkdir(parents=True)

harvest.MIN = 500
queries = [
    '"Caleb Pressley" portrait headshot',
    '"Caleb Pressley" Sundae Conversation interview',
    '"Caleb Pressley" Barstool Sports photo',
    '"Caleb Pressley" podcast interview photo',
    '"Caleb Pressley" event photo',
    '"Caleb Pressley" standing photo',
    '"Caleb Pressley" full body',
    '"Caleb Pressley" North Carolina football',
    '"Caleb Pressley" candid photo',
    '"Caleb Pressley" high resolution',
]
labels = ['portrait-front','interview','professional','context-event','candid']
tokens = harvest.toks(NAME, DISAMB)
accepted = []
phashes = []
seen_urls = set()
seen_sha = set()

for query in queries:
    if len(accepted) >= 5:
        break
    candidates = harvest.search(query, tokens)
    for c in candidates:
        if len(accepted) >= 5:
            break
        if c['image'] in seen_urls:
            continue
        got = harvest.fetch(c, 'context-event')
        if not got:
            continue
        im, final_url = got
        ph = imagehash.phash(im)
        if any(ph - old <= 6 for old in phashes):
            continue
        buf = io.BytesIO()
        im.save(buf, 'JPEG', quality=94, optimize=True, progressive=True)
        data = buf.getvalue()
        sha = hashlib.sha256(data).hexdigest()
        if sha in seen_sha:
            continue
        idx = len(accepted) + 1
        label = labels[idx - 1]
        path = FOLDER / f'{SLUG}_{idx:02d}_{label}.jpg'
        path.write_bytes(data)
        phashes.append(ph)
        seen_sha.add(sha)
        seen_urls.add(c['image'])
        accepted.append({
            'person_name': NAME,
            'disambiguation': DISAMB,
            'filename': str(path.relative_to(ROOT)),
            'image_type': label,
            'source_page_url': c['page'],
            'direct_image_url': final_url,
            'source_domain': urlparse(c['page'] or final_url).netloc,
            'width': im.width,
            'height': im.height,
            'file_format': 'JPEG',
            'sha256': sha,
            'perceptual_hash': str(ph),
            'identity_confidence': 'high',
            'identity_evidence': f'exact-name and Barstool/Sundae context search; metadata_score={harvest.score(c,tokens)}; title={c["title"][:160]}',
            'search_query': query,
            'provider': c['provider'],
            'notes': 'manual final visual review required before merge',
        })

fields = ['person_name','disambiguation','filename','image_type','source_page_url','direct_image_url','source_domain','width','height','file_format','sha256','perceptual_hash','identity_confidence','identity_evidence','search_query','provider','notes']
with (ROOT/'manifest.csv').open('w', encoding='utf-8-sig', newline='') as f:
    w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(accepted)
with (ROOT/'failures.csv').open('w', encoding='utf-8-sig', newline='') as f:
    w = csv.DictWriter(f, fieldnames=['person_name','images_accepted','images_required','reason']); w.writeheader()
    if len(accepted) != 5:
        w.writerow({'person_name':NAME,'images_accepted':len(accepted),'images_required':5,'reason':'Could not obtain five distinct public photos'})
(ROOT/'README.txt').write_text(f'People requested: 1\nPeople complete with exactly 5 images: {int(len(accepted)==5)}\nPeople failed/incomplete: {int(len(accepted)!=5)}\nAccepted images: {len(accepted)}\n')
with zipfile.ZipFile('people-photos-caleb.zip','w',zipfile.ZIP_DEFLATED) as z:
    for p in ROOT.rglob('*'):
        if p.is_file(): z.write(p,p.relative_to(ROOT.parent))
print((ROOT/'README.txt').read_text())
