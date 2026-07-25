#!/usr/bin/env python3
from __future__ import annotations

import csv
import hashlib
import html
import os
import random
import re
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

import imagehash
import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

TARGET = int(os.getenv("TARGET_COUNT", "300"))
OUT = Path("imdb300_output")
PHOTOS = OUT / "photos"
NAME_ID = "nm11646853"
UA = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/17 Safari/605.1.15",
]


def get(url, timeout=15):
    return requests.get(url, timeout=timeout, headers={"User-Agent": random.choice(UA), "Accept-Language": "en-US,en;q=0.9"})


def collect_title_ids():
    ids = set()
    urls = [
        f"https://www.imdb.com/name/{NAME_ID}/filmotype/",
        f"https://www.imdb.com/name/{NAME_ID}/fullcredits/",
        f"https://www.imdb.com/name/{NAME_ID}/",
    ]
    for u in urls:
        try:
            r = get(u, 20)
            print(f"NAME_PAGE {u} status={r.status_code} bytes={len(r.content)}", flush=True)
            if r.status_code != 200:
                continue
            ids.update(re.findall(r"/title/(tt\d{7,9})", r.text))
            ids.update(re.findall(r'"id"\s*:\s*"(tt\d{7,9})"', r.text))
        except Exception as exc:
            print(f"NAME_FAIL {u}: {exc}", flush=True)
    print(f"TITLE_IDS={len(ids)}", flush=True)
    return sorted(ids)


def clean_img_url(url: str) -> str:
    url = html.unescape(url).replace("\\u002F", "/").replace("\\/", "/")
    url = url.split(" ")[0].strip('"\' ,')
    if "._V1_" in url:
        prefix, rest = url.split("._V1_", 1)
        ext = ".jpg"
        if ".png" in rest.lower():
            ext = ".png"
        url = prefix + "._V1_QL90" + ext
    return url


def extract_images(title_id: str):
    pages = [
        f"https://www.imdb.com/title/{title_id}/mediaindex/",
        f"https://www.imdb.com/title/{title_id}/",
    ]
    urls = []
    title = ""
    for page_url in pages:
        try:
            r = get(page_url, 14)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            if not title:
                title = (soup.title.get_text(" ", strip=True) if soup.title else title_id)
            for tag in soup.select('meta[property="og:image"], meta[name="twitter:image"]'):
                v = tag.get("content")
                if v:
                    urls.append(v)
            for tag in soup.select("img"):
                for attr in ("src", "data-src", "data-imageurl"):
                    v = tag.get(attr)
                    if v:
                        urls.append(v)
                srcset = tag.get("srcset") or ""
                for part in srcset.split(","):
                    v = part.strip().split(" ")[0]
                    if v:
                        urls.append(v)
            urls.extend(re.findall(r'https://m\.media-amazon\.com/images/M/[^"\\ ]+?\.(?:jpg|jpeg|png)', r.text, flags=re.I))
        except Exception:
            continue
    out = []
    seen = set()
    for u in urls:
        u = clean_img_url(u)
        if not u.startswith("https://m.media-amazon.com/images/M/"):
            continue
        if u in seen:
            continue
        seen.add(u)
        out.append((title_id, title, u))
        if len(out) >= 7:
            break
    return out


def fetch_image(item):
    title_id, title, url = item
    try:
        r = get(url, 15)
        if r.status_code != 200 or len(r.content) < 5000 or len(r.content) > 20 * 1024 * 1024:
            return None
        with Image.open(BytesIO(r.content)) as im:
            im.load()
            im = ImageOps.exif_transpose(im).convert("RGB")
            if im.width < 300 or im.height < 180:
                return None
            ratio = im.width / im.height
            if ratio < 0.42 or ratio > 2.45:
                return None
            orientation = "landscape" if ratio >= 1.18 else ("portrait" if ratio <= 0.88 else "square")
            return title_id, title, url, im.copy(), r.content, orientation, ratio
    except Exception:
        return None


def sheet(rows):
    tw, th, lh, cols, gap = 180, 112, 22, 8, 6
    rc = (len(rows) + cols - 1) // cols
    canvas = Image.new("RGB", (gap + cols * (tw + gap), gap + rc * (th + lh + gap)), "white")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()
    for i, row in enumerate(rows):
        x = gap + (i % cols) * (tw + gap)
        y = gap + (i // cols) * (th + lh + gap)
        try:
            with Image.open(PHOTOS / row["filename"]) as im:
                canvas.paste(ImageOps.fit(im.convert("RGB"), (tw, th), method=Image.Resampling.LANCZOS), (x, y))
            draw.text((x + 3, y + th + 4), f"#{i+1:03d} {row['title_id'][-5:]}", fill="black", font=font)
        except OSError:
            pass
    canvas.save(OUT / "contact_sheet.jpg", "JPEG", quality=88, optimize=True)


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    PHOTOS.mkdir(parents=True)
    title_ids = collect_title_ids()

    all_items = []
    with ThreadPoolExecutor(max_workers=24) as ex:
        futs = [ex.submit(extract_images, tid) for tid in title_ids]
        for fut in as_completed(futs):
            all_items.extend(fut.result())
    print(f"IMAGE_URLS={len(all_items)}", flush=True)
    random.shuffle(all_items)

    fetched = []
    with ThreadPoolExecutor(max_workers=32) as ex:
        futs = [ex.submit(fetch_image, item) for item in all_items]
        for fut in as_completed(futs):
            result = fut.result()
            if result:
                fetched.append(result)
    print(f"FETCHED={len(fetched)}", flush=True)

    # Prioritize actual landscape media stills, then vertical posters, while limiting repeated title art.
    pools = {"landscape": [], "portrait": [], "square": []}
    for x in fetched:
        pools[x[5]].append(x)
    for p in pools.values():
        random.shuffle(p)

    ordered = pools["landscape"] + pools["portrait"] + pools["square"]
    rows = []
    exact = set()
    phashes = []
    per_title = {}
    for title_id, title, url, im, raw, orientation, ratio in ordered:
        if len(rows) >= TARGET:
            break
        if per_title.get(title_id, 0) >= 3:
            continue
        sha = hashlib.sha256(raw).hexdigest()
        if sha in exact:
            continue
        ph = imagehash.phash(im)
        if any((ph - old) <= 2 for old in phashes):
            continue
        exact.add(sha)
        phashes.append(ph)
        per_title[title_id] = per_title.get(title_id, 0) + 1
        idx = len(rows) + 1
        fn = f"anna_claire_clouds_imdb_{idx:03d}.jpg"
        im.save(PHOTOS / fn, "JPEG", quality=93, optimize=True, progressive=True)
        rows.append({
            "index": idx,
            "filename": fn,
            "title_id": title_id,
            "title": title,
            "orientation": orientation,
            "ratio": f"{ratio:.3f}",
            "image_url": url,
            "source_page": f"https://www.imdb.com/title/{title_id}/mediaindex/",
        })

    with (OUT / "manifest.csv").open("w", newline="", encoding="utf-8-sig") as f:
        fields = ["index", "filename", "title_id", "title", "orientation", "ratio", "image_url", "source_page"]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    sheet(rows)
    counts = {k: sum(1 for r in rows if r["orientation"] == k) for k in pools}
    (OUT / "README.txt").write_text(
        f"IMDb filmography/media harvest locked to Anna Claire Clouds ({NAME_ID}).\nCollected: {len(rows)}\n"
        f"Landscape: {counts['landscape']} | Portrait: {counts['portrait']} | Square: {counts['square']}\n"
        "Up to three distinct images per credited title; sources in manifest.csv.\n",
        encoding="utf-8",
    )
    shutil.make_archive("anna_claire_clouds_imdb_scene_300", "zip", OUT)
    Path("IMDB300_COUNT.txt").write_text(str(len(rows)), encoding="utf-8")
    print(f"FINAL_COUNT={len(rows)}", flush=True)


if __name__ == "__main__":
    main()
