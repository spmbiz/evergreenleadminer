#!/usr/bin/env python3
from __future__ import annotations

import csv
import hashlib
import html
import json
import os
import random
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from urllib.parse import quote_plus, urlparse

import imagehash
import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

TARGET = int(os.getenv("TARGET_COUNT", "300"))
OUT = Path("scene300_output")
PHOTOS = OUT / "photos"
TIMEOUT = 10
MAX_BYTES = 12 * 1024 * 1024
MIN_W = 280
MIN_H = 180
UA = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/17 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/125 Safari/537.36",
]

QUERY_TERMS = [
    "POV", "POV scene", "POV trailer", "POV preview", "POV thumbnail",
    "scene", "scene preview", "scene trailer", "scene still", "video still",
    "video thumbnail", "movie scene", "movie still", "episode", "trailer",
    "preview", "official preview", "teaser", "VR", "VR scene", "VR trailer",
    "VR preview", "4K", "HD scene", "vertical video", "reel", "short video",
    "behind the scenes", "BTS", "studio scene", "production still", "poster",
    "cover", "screencap", "screenshots", "gallery", "clip", "full scene preview",
]
QUERIES = [f'"Anna Claire Clouds" {term}' for term in QUERY_TERMS]
QUERIES += [
    '"Anna Claire Clouds" site:povr.com',
    '"Anna Claire Clouds" site:vrbangers.com',
    '"Anna Claire Clouds" site:adulttime.com',
    '"Anna Claire Clouds" site:deeper.com',
    '"Anna Claire Clouds" site:blacked.com',
    '"Anna Claire Clouds" site:vixen.com',
    '"Anna Claire Clouds" site:tushy.com',
    '"Anna Claire Clouds" site:brazzers.com',
    '"Anna Claire Clouds" site:teamskeet.com',
    '"Anna Claire Clouds" site:realitykings.com',
    '"Anna Claire Clouds" site:mofos.com',
    '"Anna Claire Clouds" site:evilangel.com',
    '"Anna Claire Clouds" site:wicked.com',
    '"Anna Claire Clouds" site:spizoo.com',
    '"Anna Claire Clouds" site:youtube.com trailer',
]

BLOCKED_TERMS = {
    "torrent", "leak", "onlyfans leak", "telegram", "rule34", "hentai",
    "deepfake", "ai nude", "fake nude", "escort", "camgirl",
}

@dataclass(frozen=True)
class Candidate:
    image_url: str
    thumb_url: str
    source_url: str
    title: str
    query: str


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", html.unescape(s).lower())


def exact_lock(c: Candidate) -> bool:
    blob = f"{c.title} {c.source_url} {c.image_url}"
    n = norm(blob)
    return "annaclaireclouds" in n or "annaclairclouds" in n


def blocked(c: Candidate) -> bool:
    blob = html.unescape(f"{c.title} {c.source_url} {c.image_url}").lower()
    return any(term in blob for term in BLOCKED_TERMS)


def bing_candidates(session: requests.Session, query: str, pages: int = 10):
    seen = set()
    for page in range(pages):
        first = page * 35 + 1
        url = (
            "https://www.bing.com/images/async"
            f"?q={quote_plus(query)}&first={first}&count=35&adlt=moderate"
            "&qft=%2Bfilterui%3Aphoto-photo"
        )
        try:
            r = session.get(url, timeout=TIMEOUT)
            r.raise_for_status()
        except requests.RequestException:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        fresh = 0
        for tag in soup.select("a.iusc[m]"):
            try:
                m = json.loads(tag.get("m") or "{}")
            except Exception:
                continue
            c = Candidate(
                image_url=str(m.get("murl") or "").strip(),
                thumb_url=str(m.get("turl") or "").strip(),
                source_url=str(m.get("purl") or "").strip(),
                title=str(m.get("t") or "").strip(),
                query=query,
            )
            key = c.image_url or c.thumb_url
            if not key.startswith("http") or key in seen:
                continue
            seen.add(key)
            if not exact_lock(c) or blocked(c):
                continue
            fresh += 1
            yield c
        print(f"SEARCH {query!r} page={page+1} fresh={fresh}", flush=True)
        if fresh == 0 and page >= 3:
            break
        time.sleep(0.12)


def fetch_one(c: Candidate):
    s = requests.Session()
    headers = {
        "User-Agent": random.choice(UA),
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Referer": c.source_url if c.source_url.startswith("http") else "https://www.bing.com/",
    }
    urls = [u for u in (c.image_url, c.thumb_url) if u.startswith("http")]
    for url in urls:
        try:
            with s.get(url, headers=headers, timeout=TIMEOUT, stream=True, allow_redirects=True) as r:
                r.raise_for_status()
                ctype = (r.headers.get("content-type") or "").lower()
                if "html" in ctype or "svg" in ctype:
                    continue
                raw = bytearray()
                for chunk in r.iter_content(65536):
                    if not chunk:
                        continue
                    raw.extend(chunk)
                    if len(raw) > MAX_BYTES:
                        break
                if not raw or len(raw) > MAX_BYTES:
                    continue
        except requests.RequestException:
            continue
        try:
            with Image.open(BytesIO(raw)) as im:
                im.load()
                im = ImageOps.exif_transpose(im).convert("RGB")
                if im.width < MIN_W or im.height < MIN_H:
                    continue
                ratio = im.width / im.height
                if ratio < 0.42 or ratio > 2.45:
                    continue
                orientation = "landscape" if ratio >= 1.18 else ("portrait" if ratio <= 0.88 else "square")
                return c, im.copy(), bytes(raw), orientation, ratio, url
        except (UnidentifiedImageError, OSError, ValueError):
            continue
    return None


def make_sheet(rows: list[dict[str, str]]):
    tw, th, label_h, cols, gap = 180, 120, 22, 8, 6
    rcount = (len(rows) + cols - 1) // cols
    canvas = Image.new("RGB", (gap + cols * (tw + gap), gap + rcount * (th + label_h + gap)), "white")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()
    for i, row in enumerate(rows):
        x = gap + (i % cols) * (tw + gap)
        y = gap + (i // cols) * (th + label_h + gap)
        try:
            with Image.open(PHOTOS / row["filename"]) as im:
                thumb = ImageOps.fit(im.convert("RGB"), (tw, th), method=Image.Resampling.LANCZOS)
                canvas.paste(thumb, (x, y))
            draw.text((x + 3, y + th + 4), f"#{i+1:03d} {row['orientation'][0].upper()}", fill="black", font=font)
        except OSError:
            pass
    canvas.save(OUT / "contact_sheet.jpg", "JPEG", quality=88, optimize=True)


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    PHOTOS.mkdir(parents=True)

    s = requests.Session()
    s.headers.update({"User-Agent": random.choice(UA), "Accept-Language": "en-US,en;q=0.9"})

    candidates = []
    seen_urls = set()
    for q in QUERIES:
        for c in bing_candidates(s, q):
            key = c.image_url or c.thumb_url
            if key in seen_urls:
                continue
            seen_urls.add(key)
            candidates.append(c)
        if len(candidates) >= 1500:
            break
    print(f"CANDIDATES={len(candidates)}", flush=True)

    fetched = []
    with ThreadPoolExecutor(max_workers=32) as ex:
        futs = [ex.submit(fetch_one, c) for c in candidates]
        for fut in as_completed(futs):
            result = fut.result()
            if result:
                fetched.append(result)
            if len(fetched) >= 520:
                break
    print(f"FETCHED={len(fetched)}", flush=True)

    random.shuffle(fetched)
    exact_hashes = set()
    phashes = []
    pools = {"landscape": [], "portrait": [], "square": []}
    for c, im, raw, orientation, ratio, used_url in fetched:
        sha = hashlib.sha256(raw).hexdigest()
        if sha in exact_hashes:
            continue
        ph = imagehash.phash(im)
        if any((ph - old) <= 2 for old in phashes):
            continue
        exact_hashes.add(sha)
        phashes.append(ph)
        pools[orientation].append((c, im, raw, orientation, ratio, used_url))

    selected = []
    for orientation, quota in (("landscape", 240), ("portrait", 45), ("square", 15)):
        selected.extend(pools[orientation][:quota])
    if len(selected) < TARGET:
        used_ids = {id(x) for x in selected}
        leftovers = [x for p in pools.values() for x in p if id(x) not in used_ids]
        selected.extend(leftovers[: TARGET - len(selected)])
    selected = selected[:TARGET]

    rows = []
    for idx, (c, im, raw, orientation, ratio, used_url) in enumerate(selected, 1):
        fn = f"anna_claire_clouds_scene_{idx:03d}.jpg"
        im.save(PHOTOS / fn, "JPEG", quality=92, optimize=True, progressive=True)
        rows.append({
            "index": idx,
            "filename": fn,
            "orientation": orientation,
            "ratio": f"{ratio:.3f}",
            "query": c.query,
            "title": c.title,
            "source_page": c.source_url,
            "image_url": c.image_url,
            "downloaded_url": used_url,
            "domain": urlparse(c.source_url).netloc.lower(),
        })

    with (OUT / "manifest.csv").open("w", newline="", encoding="utf-8-sig") as f:
        fields = ["index", "filename", "orientation", "ratio", "query", "title", "source_page", "image_url", "downloaded_url", "domain"]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    make_sheet(rows)
    counts = {k: sum(1 for r in rows if r["orientation"] == k) for k in pools}
    (OUT / "README.txt").write_text(
        "Public search-result scene thumbnails/poster stills locked to Anna Claire Clouds.\n"
        f"Collected: {len(rows)}\n"
        f"Landscape: {counts['landscape']} | Portrait: {counts['portrait']} | Square: {counts['square']}\n"
        "Sources and exact queries are in manifest.csv.\n",
        encoding="utf-8",
    )
    shutil.make_archive("anna_claire_clouds_300_scene_stills", "zip", OUT)
    Path("SCENE300_COUNT.txt").write_text(str(len(rows)), encoding="utf-8")
    print(f"FINAL_COUNT={len(rows)}", flush=True)


if __name__ == "__main__":
    main()
