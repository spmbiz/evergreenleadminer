#!/usr/bin/env python3
from __future__ import annotations

import csv, hashlib, html, json, os, random, shutil, sys, time
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from urllib.parse import quote_plus, urlparse

import imagehash
import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

TARGET = int(os.getenv("TARGET_COUNT", "200"))
OUT = Path("output")
PHOTOS = OUT / "photos"
MIN_SIDE = 300
MAX_BYTES = 18 * 1024 * 1024
TIMEOUT = 18

QUERIES = [
    '"Anna Claire Clouds" portrait', '"Anna Claire Clouds" red carpet',
    '"Anna Claire Clouds" awards', '"Anna Claire Clouds" interview',
    '"Anna Claire Clouds" selfie', '"Anna Claire Clouds" fashion',
    '"Anna Claire Clouds" event', '"Anna Claire Clouds" Instagram',
    '"Anna Claire Clouds" TikTok', '"Anna Claire Clouds" podcast',
    '"Anna Claire Clouds" headshot', '"Anna Claire Clouds" model',
    '"Anna Claire Clouds" photoshoot', '"Anna Claire Clouds" premiere',
    '"Anna Claire Clouds" 2026', '"Anna Claire Clouds" 2025',
    '"Anna Claire Clouds" 2024', '"Anna Claire Clouds" 2023',
    '"annaclairecloudstv"', '"annaclaireclouds"',
    'site:instagram.com "Anna Claire Clouds"',
    'site:facebook.com "Anna Claire Clouds"',
    'site:pinterest.com "Anna Claire Clouds"',
]

BLOCKED = {
    "porn", "porno", "xxx", "xhamster", "xvideos", "xnxx", "redtube",
    "brazzers", "bangbros", "tushy", "blacked", "vixen", "deeper",
    "onlyfans", "fansly", "manyvids", "nude", "naked", "leak", "torrent",
    "telegram", "camgirl", "escort", "hentai", "rule34", "julesjordan",
    "adulttime", "realitykings", "mofos", "teamskeet", "pornhub",
    "spankbang", "naughtyamerica", "evilangel", "slayed", "sexscene",
}

UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/17 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/125 Safari/537.36",
]

@dataclass(frozen=True)
class Candidate:
    image_url: str
    source_url: str
    title: str
    query: str


def is_blocked(*values: str) -> bool:
    s = html.unescape(" ".join(values)).lower()
    return any(term in s for term in BLOCKED)


def get_candidates(session: requests.Session, query: str, pages: int = 13):
    seen = set()
    for page in range(pages):
        first = 1 + page * 35
        url = ("https://www.bing.com/images/async?"
               f"q={quote_plus(query)}&first={first}&count=35&adlt=strict"
               "&qft=%2Bfilterui%3Aphoto-photo")
        try:
            r = session.get(url, timeout=TIMEOUT)
            r.raise_for_status()
        except requests.RequestException as e:
            print("SEARCH_ERROR", query, page, e, file=sys.stderr)
            time.sleep(1.5)
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        fresh = 0
        for a in soup.select("a.iusc[m]"):
            try:
                m = json.loads(a.get("m", "{}"))
            except json.JSONDecodeError:
                continue
            img = str(m.get("murl") or "").strip()
            src = str(m.get("purl") or "").strip()
            title = str(m.get("t") or m.get("desc") or "").strip()
            if not img.startswith(("http://", "https://")) or img in seen:
                continue
            if is_blocked(img, src, title):
                continue
            seen.add(img); fresh += 1
            yield Candidate(img, src, title, query)
        print(f"SEARCH {query!r} page={page+1} fresh={fresh}")
        if fresh == 0:
            break
        time.sleep(0.7 + random.random() * 0.5)


def download(session: requests.Session, c: Candidate):
    headers = {"User-Agent": random.choice(UAS), "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"}
    if c.source_url.startswith("http"):
        headers["Referer"] = c.source_url
    try:
        with session.get(c.image_url, headers=headers, timeout=TIMEOUT, stream=True, allow_redirects=True) as r:
            r.raise_for_status()
            ct = (r.headers.get("content-type") or "").lower()
            if "html" in ct or "svg" in ct:
                return None
            buf = bytearray()
            for chunk in r.iter_content(65536):
                if chunk:
                    buf.extend(chunk)
                    if len(buf) > MAX_BYTES:
                        return None
    except requests.RequestException:
        return None
    try:
        with Image.open(BytesIO(buf)) as im:
            im.load(); im = ImageOps.exif_transpose(im)
            if min(im.size) < MIN_SIDE:
                return None
            if max(im.width / im.height, im.height / im.width) > 3.2:
                return None
            if im.mode != "RGB":
                if "A" in im.getbands():
                    bg = Image.new("RGB", im.size, "white")
                    bg.paste(im, mask=im.getchannel("A")); im = bg
                else:
                    im = im.convert("RGB")
            return im.copy(), bytes(buf)
    except (UnidentifiedImageError, OSError, ValueError):
        return None


def near_dup(h, hashes):
    return any((h - old) <= 1 for old in hashes)


def make_sheet(rows):
    thumb, gap, label, cols = 150, 8, 22, 10
    nrows = (len(rows) + cols - 1) // cols
    sheet = Image.new("RGB", (gap + cols*(thumb+gap), gap + nrows*(thumb+label+gap)), "white")
    draw = ImageDraw.Draw(sheet); font = ImageFont.load_default()
    for i, row in enumerate(rows):
        x = gap + (i % cols)*(thumb+gap); y = gap + (i // cols)*(thumb+label+gap)
        with Image.open(PHOTOS / row["filename"]) as im:
            tile = ImageOps.fit(im.convert("RGB"), (thumb, thumb), method=Image.Resampling.LANCZOS)
        sheet.paste(tile, (x, y)); draw.text((x+3, y+thumb+3), f"#{i+1:03d}", fill="black", font=font)
    sheet.save(OUT / "contact_sheet.jpg", "JPEG", quality=88, optimize=True)


def main():
    if OUT.exists(): shutil.rmtree(OUT)
    PHOTOS.mkdir(parents=True)
    s = requests.Session(); s.headers.update({"User-Agent": random.choice(UAS), "Accept-Language": "en-US,en;q=0.9"})
    sha_seen, phashes, rows = set(), [], []
    attempted = 0
    for q in QUERIES:
        if len(rows) >= TARGET: break
        for c in get_candidates(s, q):
            if len(rows) >= TARGET: break
            attempted += 1
            got = download(s, c)
            if not got: continue
            im, raw = got
            sha = hashlib.sha256(raw).hexdigest()
            if sha in sha_seen: continue
            ph = imagehash.phash(im)
            if near_dup(ph, phashes): continue
            idx = len(rows) + 1
            fn = f"anna_claire_clouds_{idx:03d}.jpg"
            im.save(PHOTOS / fn, "JPEG", quality=93, optimize=True, progressive=True)
            sha_seen.add(sha); phashes.append(ph)
            rows.append({
                "index": idx, "filename": fn, "query": c.query, "title": c.title,
                "source_page": c.source_url, "image_url": c.image_url,
                "width": im.width, "height": im.height, "sha256_original": sha,
                "source_domain": urlparse(c.source_url or c.image_url).netloc,
            })
            print(f"ACCEPT {idx}/{TARGET} {im.width}x{im.height} {rows[-1]['source_domain']}")
            time.sleep(0.05)
    fields = ["index","filename","query","title","source_page","image_url","width","height","sha256_original","source_domain"]
    with (OUT / "manifest.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(rows)
    if rows: make_sheet(rows)
    (OUT / "README.txt").write_text(
        f"Anna Claire Clouds public-web photo set\nCollected: {len(rows)} / {TARGET}\nCandidates attempted: {attempted}\n\n"
        "SafeSearch=strict; explicit/paywalled-looking sources blocked; minimum 300 px per side; corrupt files rejected; SHA-256 and near-exact perceptual duplicates removed.\n"
        "Source pages and direct image URLs are recorded in manifest.csv. Rights remain with their owners; verify permission before publication or commercial use.\n",
        encoding="utf-8")
    shutil.make_archive("anna_claire_clouds_200_public_photos", "zip", OUT)
    print("DONE", len(rows))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
