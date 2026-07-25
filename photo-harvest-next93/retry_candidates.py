#!/usr/bin/env python3
from __future__ import annotations
import csv, hashlib, io, json, os, shutil, zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse
import imagehash
from PIL import Image, ImageDraw, ImageFont
import harvest_next as h

BATCH = int(os.environ.get("RETRY_BATCH_INDEX","0"))
SIZE = 6
ROOT = Path(f"retry-candidates-batch-{BATCH:02d}")
ZIP = Path(f"retry-candidates-batch-{BATCH:02d}.zip")
MAX_CANDIDATES = 12

STRICT_BAD = (
    "collage","before and after","then and now","album cover","book cover","poster",
    "wallpaper","illustration","painting","drawing","meme","fan art","tribute",
    "action figure","wax figure","lookalike","impersonator","merch","t-shirt","hoodie",
    "birthday","logo","graphic design","cover art","compilation"
)
SOLO_HINTS = ("portrait","headshot","red carpet","arrives","attends","speaks","interview",
              "press conference","premiere","festival","awards","on stage","performs")


def candidate_clean(c: dict) -> bool:
    blob = (c.get("title","")+" "+c.get("page","")+" "+c.get("image","")+" "+c.get("description","")).lower()
    return not any(x in blob for x in STRICT_BAD)


def collect(person: dict) -> dict:
    name=person["name"]; context=person.get("disambiguation",""); s=h.slug(name)
    folder=ROOT/s; folder.mkdir(parents=True, exist_ok=True)
    queries=[
        f'"{name}" {context} solo portrait photo',
        f'"{name}" {context} headshot high resolution',
        f'"{name}" {context} red carpet solo',
        f'"{name}" {context} event press photo',
        f'"{name}" {context} interview photo',
        f'"{name}" {context} speaking on stage',
        f'"{name}" {context} full body standing',
        f'"{name}" {context} candid professional',
        f'"{name}" {context} official photo',
        f'"{name}" {context} portrait',
    ]
    raw=[]; seen=set()
    for q in queries:
        for c in h.search(q, person):
            key=c["image"].split("?")[0]
            if key in seen or not candidate_clean(c): continue
            seen.add(key)
            blob=(c.get("title","")+" "+c.get("page","")).lower()
            solo_bonus=sum(x in blob for x in SOLO_HINTS)
            raw.append((h.identity_score(c,person)+solo_bonus,c))
    raw.sort(key=lambda x:-x[0])
    accepted=[]; phs=[]; shas=set()
    for score,c in raw:
        if len(accepted)>=MAX_CANDIDATES: break
        got=h.fetch_image(c,"context-event",person)
        if not got: continue
        im,url,detail=got
        ph=imagehash.phash(im)
        if any(ph-old<=10 for old in phs): continue
        buf=io.BytesIO(); im.save(buf,"JPEG",quality=94,optimize=True,progressive=True)
        data=buf.getvalue(); sha=hashlib.sha256(data).hexdigest()
        if sha in shas: continue
        idx=len(accepted)+1
        p=folder/f"{s}_{idx:02d}_candidate.jpg"; p.write_bytes(data)
        accepted.append({
            "person_name":name,"disambiguation":context,"candidate_index":idx,
            "filename":str(p.relative_to(ROOT)),"source_page_url":c.get("page",""),
            "direct_image_url":url,"source_domain":urlparse(c.get("page") or url).netloc,
            "width":im.width,"height":im.height,"sha256":sha,"perceptual_hash":str(ph),
            "metadata_score":score,"title":c.get("title",""),"query":c.get("query",""),
            "provider":c.get("provider","")
        })
        phs.append(ph); shas.add(sha)
    print(f"[{name}] {len(accepted)} candidates",flush=True)
    return {"person":person,"rows":accepted}


def sheet(results):
    tw,th=170,170; left=190; block=390; cols=6
    canvas=Image.new("RGB",(left+cols*tw,max(1,len(results))*block),"white")
    draw=ImageDraw.Draw(canvas); font=ImageFont.load_default()
    for ri,res in enumerate(results):
        y0=ri*block
        draw.text((8,y0+8),res["person"]["name"],fill="black",font=font)
        draw.text((8,y0+28),f'{len(res["rows"])} candidates',fill="black",font=font)
        for i,row in enumerate(res["rows"]):
            r=i//cols;c=i%cols;x=left+c*tw;y=y0+r*190
            try:
                im=Image.open(ROOT/row["filename"]).convert("RGB")
                im.thumbnail((tw-8,th-8))
                tile=Image.new("RGB",(tw,th),"#eeeeee")
                tile.paste(im,((tw-im.width)//2,(th-im.height)//2))
                canvas.paste(tile,(x,y))
                draw.text((x+4,y+173),f'#{row["candidate_index"]}',fill="black",font=font)
            except: pass
    canvas.save(ROOT/"contact_sheet.jpg",quality=90)


def main():
    people=json.load(open("retry_people.json",encoding="utf-8"))
    people=people[BATCH*SIZE:BATCH*SIZE+SIZE]
    if ROOT.exists(): shutil.rmtree(ROOT)
    ROOT.mkdir()
    results=[]
    with ThreadPoolExecutor(max_workers=3) as ex:
        fs={ex.submit(collect,p):p for p in people}
        for f in as_completed(fs):
            try: results.append(f.result())
            except Exception as e:
                p=fs[f]; print(f'[{p["name"]}] ERROR {e!r}',flush=True)
                results.append({"person":p,"rows":[]})
    order={p["name"]:i for i,p in enumerate(people)}
    results.sort(key=lambda x:order[x["person"]["name"]])
    rows=[r for x in results for r in x["rows"]]
    fields=["person_name","disambiguation","candidate_index","filename","source_page_url",
            "direct_image_url","source_domain","width","height","sha256","perceptual_hash",
            "metadata_score","title","query","provider"]
    with (ROOT/"manifest.csv").open("w",encoding="utf-8-sig",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
    (ROOT/"README.txt").write_text(f"People: {len(people)}\nCandidates: {len(rows)}\n",encoding="utf-8")
    sheet(results)
    with zipfile.ZipFile(ZIP,"w",zipfile.ZIP_DEFLATED) as z:
        for p in ROOT.rglob("*"):
            if p.is_file(): z.write(p,p.relative_to(ROOT.parent))

if __name__=="__main__": main()
