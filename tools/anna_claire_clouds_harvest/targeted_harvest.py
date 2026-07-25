#!/usr/bin/env python3
from __future__ import annotations

import csv, hashlib, html, json, os, random, re, shutil, time
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from urllib.parse import quote_plus, urlparse

import imagehash
import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

TARGET = int(os.getenv("TARGET_COUNT", "200"))
OUT = Path("targeted_output")
PHOTOS = OUT / "photos"
TIMEOUT = 14
MIN_SIDE = 240
MAX_BYTES = 18 * 1024 * 1024

QUERIES = [
    '"Anna Claire Clouds" interview',
    '"Anna Claire Clouds" podcast',
    '"Anna Claire Clouds" awards',
    '"Anna Claire Clouds" red carpet',
    '"Anna Claire Clouds" event',
    '"Anna Claire Clouds" expo',
    '"Anna Claire Clouds" gallery',
    '"Anna Claire Clouds" portrait',
    '"Anna Claire Clouds" "AVN Awards"',
    '"Anna Claire Clouds" "XBIZ Awards"',
    '"Anna Claire Clouds" "XMA Awards"',
    '"Anna Claire Clouds" "Adult Entertainment Expo"',
    '"Anna Claire Clouds" "Holly Randall"',
    '"Anna Claire Clouds" "Pillow Talk"',
    '"Anna Claire Clouds" "Adult Time Podcast"',
    '"Anna Claire Clouds" 2026 event',
    '"Anna Claire Clouds" 2025 event',
    '"Anna Claire Clouds" 2024 event',
    '"Anna Claire Clouds" 2023 event',
    '"Anna Claire Clouds" site:xbiz.com interview',
    '"Anna Claire Clouds" site:xbiz.com awards',
    '"Anna Claire Clouds" site:avn.com interview',
    '"Anna Claire Clouds" site:avn.com awards',
    '"Anna Claire Clouds" site:gettyimages.com',
    '"Anna Claire Clouds" site:gettyimages.be',
    '"Anna Claire Clouds" site:youtube.com interview',
    '"Anna Claire Clouds" site:youtube.com podcast',
    '"Anna Claire Clouds" site:podcasts.apple.com',
    '"Anna Claire Clouds" site:open.spotify.com',
    '"Anna Claire Clouds" site:adulttheculture.com',
    '"Anna Claire Clouds" site:adultentertainmentexpo.com',
    '"Anna Claire Clouds" site:news.com.au',
    '"Anna Claire Clouds" site:wikimedia.org',
]

ALLOWED_DOMAINS = {
    "xbiz.com", "avn.com", "gettyimages.com", "gettyimages.be", "gettyimages.co.uk",
    "youtube.com", "youtu.be", "ytimg.com", "googleusercontent.com",
    "podcasts.apple.com", "mzstatic.com", "open.spotify.com", "scdn.co",
    "adulttheculture.com", "adultentertainmentexpo.com", "x3mag.com",
    "hollyrandallunfiltered.com", "themakingofaspieglergirl.com",
    "wikimedia.org", "wikipedia.org", "imdb.com", "media-amazon.com",
    "news.com.au", "shutterstock.com", "alamy.com", "podchaser.com",
    "listennotes.com", "soundcloud.com", "iheart.com", "audacy.com",
    "podbean.com", "buzzsprout.com", "simplecast.com", "libsyn.com",
}

BLOCKED = {
    "pornhub", "xvideos", "xnxx", "xhamster", "spankbang", "redtube",
    "onlyfans", "fansly", "manyvids", "leak", "torrent", "rule34",
    "nude", "naked", "hardcore", "gangbang", "blowjob", "anal-sex",
    "sex-scene", "sex_scene", "full-scene", "full_scene", "xxx-video",
}

UA = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/17 Safari/605.1.15",
]

@dataclass(frozen=True)
class Candidate:
    image_url: str
    source_url: str
    title: str
    query: str


def host(url: str) -> str:
    try: return urlparse(url).netloc.lower().split(":")[0].removeprefix("www.")
    except Exception: return ""


def allowed_domain(h: str) -> bool:
    return any(h == d or h.endswith("." + d) for d in ALLOWED_DOMAINS)


def exact_name_lock(c: Candidate) -> bool:
    text = html.unescape(f"{c.title} {c.source_url} {c.image_url}").lower()
    compact = re.sub(r"[^a-z]", "", text)
    return ("anna claire clouds" in text or "annaclaireclouds" in compact or
            "anna-claire-clouds" in text or "annaclairclouds" in compact)


def blocked(c: Candidate) -> bool:
    text = html.unescape(f"{c.title} {c.source_url} {c.image_url}").lower()
    return any(term in text for term in BLOCKED)


def bing(s: requests.Session, query: str, pages: int = 7):
    seen = set()
    for page in range(pages):
        first = page * 35 + 1
        url = f"https://www.bing.com/images/async?q={quote_plus(query)}&first={first}&count=35&adlt=strict&qft=%2Bfilterui%3Aphoto-photo"
        try:
            r = s.get(url, timeout=TIMEOUT)
            r.raise_for_status()
        except requests.RequestException:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        fresh = 0
        for tag in soup.select("a.iusc[m]"):
            try: m = json.loads(tag.get("m") or "{}")
            except Exception: continue
            c = Candidate(str(m.get("murl") or ""), str(m.get("purl") or ""), str(m.get("t") or ""), query)
            if not c.image_url.startswith("http") or c.image_url in seen: continue
            seen.add(c.image_url)
            sh, ih = host(c.source_url), host(c.image_url)
            if not (allowed_domain(sh) or allowed_domain(ih)): continue
            if not exact_name_lock(c) or blocked(c): continue
            fresh += 1
            yield c
        print(f"{query} page {page+1}: {fresh}", flush=True)
        if fresh == 0 and page >= 2: break
        time.sleep(.25)


def fetch_image(s: requests.Session, c: Candidate):
    headers = {"User-Agent": random.choice(UA), "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"}
    if c.source_url.startswith("http"): headers["Referer"] = c.source_url
    try:
        with s.get(c.image_url, headers=headers, timeout=TIMEOUT, stream=True, allow_redirects=True) as r:
            r.raise_for_status()
            ctype = (r.headers.get("content-type") or "").lower()
            if "html" in ctype or "svg" in ctype: return None
            raw = bytearray()
            for chunk in r.iter_content(65536):
                raw.extend(chunk)
                if len(raw) > MAX_BYTES: return None
    except requests.RequestException:
        return None
    try:
        with Image.open(BytesIO(raw)) as im:
            im.load(); im = ImageOps.exif_transpose(im).convert("RGB")
            if min(im.size) < MIN_SIDE: return None
            if max(im.width / im.height, im.height / im.width) > 3.8: return None
            return im.copy(), bytes(raw)
    except (UnidentifiedImageError, OSError, ValueError):
        return None


def sheet(rows):
    size, label, cols, gap = 150, 22, 10, 6
    hrows = (len(rows)+cols-1)//cols
    canvas = Image.new("RGB", (gap+cols*(size+gap), gap+hrows*(size+label+gap)), "white")
    draw, font = ImageDraw.Draw(canvas), ImageFont.load_default()
    for i, row in enumerate(rows):
        x = gap+(i%cols)*(size+gap); y = gap+(i//cols)*(size+label+gap)
        try:
            with Image.open(PHOTOS/row["filename"]) as im:
                canvas.paste(ImageOps.fit(im.convert("RGB"),(size,size),method=Image.Resampling.LANCZOS),(x,y))
            draw.text((x+3,y+size+3),f"#{i+1:03d} {row['domain'][:16]}",fill="black",font=font)
        except OSError: pass
    canvas.save(OUT/"contact_sheet.jpg",quality=88,optimize=True)


def main():
    if OUT.exists(): shutil.rmtree(OUT)
    PHOTOS.mkdir(parents=True)
    s = requests.Session(); s.headers.update({"User-Agent": random.choice(UA), "Accept-Language":"en-US,en;q=0.9"})
    exact, phashes, rows, seen_urls = set(), [], [], set()
    for q in QUERIES:
        if len(rows) >= TARGET: break
        for c in bing(s,q):
            if len(rows) >= TARGET: break
            if c.image_url in seen_urls: continue
            seen_urls.add(c.image_url)
            got = fetch_image(s,c)
            if not got: continue
            im, raw = got
            sha = hashlib.sha256(raw).hexdigest()
            if sha in exact: continue
            ph = imagehash.phash(im)
            if any(ph-old <= 3 for old in phashes): continue
            idx=len(rows)+1; fn=f"anna_claire_clouds_{idx:03d}.jpg"
            im.save(PHOTOS/fn,"JPEG",quality=92,optimize=True,progressive=True)
            exact.add(sha); phashes.append(ph)
            rows.append({"index":idx,"filename":fn,"domain":host(c.source_url) or host(c.image_url),"title":c.title,"source_page":c.source_url,"image_url":c.image_url,"query":c.query})
            print(f"accepted {idx}: {rows[-1]['domain']} | {c.title[:80]}",flush=True)
    with (OUT/"manifest.csv").open("w",newline="",encoding="utf-8-sig") as f:
        w=csv.DictWriter(f,fieldnames=["index","filename","domain","title","source_page","image_url","query"]); w.writeheader(); w.writerows(rows)
    sheet(rows)
    (OUT/"README.txt").write_text(f"Targeted exact-name public editorial/interview/event harvest.\nCollected: {len(rows)} images.\nSources are recorded in manifest.csv.\n",encoding="utf-8")
    shutil.make_archive("anna_claire_clouds_targeted_public_photos","zip",OUT)
    print(f"FINAL_COUNT={len(rows)}",flush=True)
    return 0

if __name__ == "__main__": raise SystemExit(main())
