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
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from urllib.parse import quote_plus, urljoin, urlparse

import imagehash
import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

TARGET = int(os.getenv("TARGET_COUNT", "300"))
OUT = Path("ddg_pov_output")
PHOTOS = OUT / "photos"
TIMEOUT = 12
MAX_BYTES = 14 * 1024 * 1024
MIN_SIDE = 160

QUERIES = [
    '"Anna Claire Clouds" POV',
    '"Anna Claire Clouds" POV scene',
    '"Anna Claire Clouds" POV preview',
    '"Anna Claire Clouds" POV trailer',
    '"Anna Claire Clouds" POV thumbnail',
    '"Anna Claire Clouds" POV video',
    '"Anna Claire Clouds" POVR',
    '"Anna Claire Clouds" "Happy Little Clouds"',
    '"Anna Claire Clouds" "A Girl and Her Canvas"',
    '"Anna Claire Clouds" "Intimately POV"',
    '"Anna Claire Clouds" "POV Hookups"',
    '"Anna Claire Clouds" "Mr Big POV"',
    '"Anna Claire Clouds" "Manuel’s Fucking POV 14"',
    '"Anna Claire Clouds" "Manuel\'s Fucking POV 14"',
    '"Anna Claire Clouds" "VR Bangers"',
    '"Anna Claire Clouds" "A Huge Fan"',
    '"Anna Claire Clouds" "Double Delight"',
    '"Anna Claire Clouds" VR scene',
    '"Anna Claire Clouds" VR preview',
    '"Anna Claire Clouds" VR trailer',
    '"Anna Claire Clouds" virtual reality',
    '"Anna Claire Clouds" scene preview',
    '"Anna Claire Clouds" scene thumbnail',
    '"Anna Claire Clouds" video still',
    '"Anna Claire Clouds" trailer',
    '"Anna Claire Clouds" preview',
    '"Anna Claire Clouds" scene',
    '"Anna Claire Clouds" Dark Side trailer',
    '"Anna Claire Clouds" Cassex trailer',
    '"Anna Claire Clouds" POV award scene',
    '"Ana Clouds" POV',
    '"Anna Clouds" POV',
]

KNOWN_TITLES = [
    "happy little clouds", "a girl and her canvas", "intimately pov", "pov hookups",
    "mr big pov", "manuel's fucking pov 14", "manuels fucking pov 14", "vr bangers",
    "a huge fan", "double delight", "dark side", "cassex",
]

GOOD_DOMAINS = {
    "xbiz.com", "avn.com", "imdb.com", "vimeo.com", "youtube.com", "youtu.be",
    "ytimg.com", "intimatelypov.net", "povr.com", "vrbangers.com", "mrbigpov.com",
    "adulttheculture.com", "wikimedia.org", "wikipedia.org", "iafd.com",
}

GENERIC_BAD = {
    "airplane", "aircraft", "cloudscape", "weather", "sky", "aviation", "flight",
    "storm cloud", "cumulus", "cloud computing", "icloud", "cloud storage", "aws cloud",
    "cat", "kitten", "dog", "cake", "dessert", "wallpaper", "landscape",
}

UA = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/17 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/125 Safari/537.36",
]

@dataclass
class Candidate:
    engine: str
    query: str
    rank: int
    title: str
    source_page: str
    image_url: str
    thumbnail_url: str


def compact(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", html.unescape(s).lower())


def host(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().split(":")[0].removeprefix("www.")
    except Exception:
        return ""


def domain_good(h: str) -> bool:
    return any(h == d or h.endswith("." + d) for d in GOOD_DOMAINS)


def relevance(c: Candidate) -> int:
    text = html.unescape(f"{c.title} {c.source_page} {c.image_url}").lower()
    ctext = compact(text)
    score = 0
    if "annaclaireclouds" in ctext or "annaclairclouds" in ctext:
        score += 8
    elif "anaclouds" in ctext or "annaclouds" in ctext:
        score += 5
    for title in KNOWN_TITLES:
        if compact(title) in ctext:
            score += 6
    if "pov" in text:
        score += 3
    if "vr" in text or "virtual reality" in text:
        score += 2
    if domain_good(host(c.source_page)) or domain_good(host(c.image_url)):
        score += 2
    if any(term in text for term in GENERIC_BAD) and score < 8:
        return -10
    return score


def ddg_vqd(s: requests.Session, query: str) -> str | None:
    try:
        r = s.get("https://duckduckgo.com/", params={"q": query, "iax": "images", "ia": "images"}, timeout=TIMEOUT)
        r.raise_for_status()
    except requests.RequestException:
        return None
    patterns = [r'vqd=["\']([\d-]+)', r'vqd=([\d-]+)&', r'"vqd":"([^"]+)"']
    for p in patterns:
        m = re.search(p, r.text)
        if m:
            return m.group(1)
    return None


def ddg_candidates(s: requests.Session, query: str, max_pages: int = 5):
    vqd = ddg_vqd(s, query)
    if not vqd:
        print(f"[ddg] no vqd for {query}", flush=True)
        return
    url = "https://duckduckgo.com/i.js"
    params = {"l": "us-en", "o": "json", "q": query, "vqd": vqd, "f": ",,,", "p": "-1"}
    rank = 0
    for page in range(max_pages):
        try:
            r = s.get(url, params=params if page == 0 else None, timeout=TIMEOUT, headers={"Referer": "https://duckduckgo.com/"})
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError) as exc:
            print(f"[ddg] error {query} page {page+1}: {exc}", flush=True)
            break
        results = data.get("results") or []
        fresh = 0
        for item in results:
            rank += 1
            c = Candidate(
                engine="duckduckgo",
                query=query,
                rank=rank,
                title=str(item.get("title") or ""),
                source_page=str(item.get("url") or ""),
                image_url=str(item.get("image") or ""),
                thumbnail_url=str(item.get("thumbnail") or ""),
            )
            if relevance(c) >= 7:
                fresh += 1
                yield c
        print(f"[ddg] {query} page {page+1}: {fresh}/{len(results)} relevant", flush=True)
        nxt = data.get("next")
        if not nxt or not results:
            break
        url = urljoin("https://duckduckgo.com", nxt)
        params = None
        time.sleep(0.15 + random.random() * 0.15)


def bing_candidates(s: requests.Session, query: str, max_pages: int = 4):
    rank = 0
    for page in range(max_pages):
        first = page * 35 + 1
        url = f"https://www.bing.com/images/async?q={quote_plus(query)}&first={first}&count=35&adlt=off&qft=%2Bfilterui%3Aphoto-photo"
        try:
            r = s.get(url, timeout=TIMEOUT)
            r.raise_for_status()
        except requests.RequestException:
            break
        soup = BeautifulSoup(r.text, "html.parser")
        fresh = 0
        for tag in soup.select("a.iusc[m]"):
            try:
                m = json.loads(tag.get("m") or "{}")
            except Exception:
                continue
            rank += 1
            c = Candidate(
                engine="bing",
                query=query,
                rank=rank,
                title=str(m.get("t") or ""),
                source_page=str(m.get("purl") or ""),
                image_url=str(m.get("murl") or ""),
                thumbnail_url=str(m.get("turl") or ""),
            )
            if relevance(c) >= 7:
                fresh += 1
                yield c
        print(f"[bing] {query} page {page+1}: {fresh} relevant", flush=True)
        time.sleep(0.15)


def fetch_image(s: requests.Session, urls: list[str], referer: str):
    for url in urls:
        if not url.startswith("http"):
            continue
        headers = {"User-Agent": random.choice(UA), "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"}
        if referer.startswith("http"):
            headers["Referer"] = referer
        try:
            with s.get(url, headers=headers, timeout=TIMEOUT, stream=True, allow_redirects=True) as r:
                r.raise_for_status()
                ctype = (r.headers.get("content-type") or "").lower()
                if "html" in ctype or "svg" in ctype:
                    continue
                raw = bytearray()
                for chunk in r.iter_content(65536):
                    if chunk:
                        raw.extend(chunk)
                    if len(raw) > MAX_BYTES:
                        raw = bytearray()
                        break
            if not raw:
                continue
            with Image.open(BytesIO(raw)) as im:
                im.load()
                im = ImageOps.exif_transpose(im).convert("RGB")
                if min(im.size) < MIN_SIDE:
                    continue
                if max(im.width / im.height, im.height / im.width) > 4.5:
                    continue
                return im.copy(), bytes(raw), url
        except (requests.RequestException, UnidentifiedImageError, OSError, ValueError):
            continue
    return None


def page_images(s: requests.Session, c: Candidate):
    if not c.source_page.startswith("http"):
        return []
    try:
        r = s.get(c.source_page, timeout=TIMEOUT, headers={"User-Agent": random.choice(UA)})
        r.raise_for_status()
    except requests.RequestException:
        return []
    text = html.unescape(r.text).lower()
    if not ("anna claire clouds" in text or "annaclaireclouds" in compact(text) or any(t in text for t in KNOWN_TITLES)):
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    urls = []
    for key, attr in [("meta[property='og:image']", "content"), ("meta[name='twitter:image']", "content")]:
        for tag in soup.select(key):
            u = urljoin(c.source_page, tag.get(attr) or "")
            if u.startswith("http"):
                urls.append(u)
    for tag in soup.select("video[poster], img[src], img[data-src], source[src]"):
        u = tag.get("poster") or tag.get("src") or tag.get("data-src") or ""
        u = urljoin(c.source_page, u)
        if u.startswith("http"):
            urls.append(u)
    seen = set()
    return [u for u in urls if not (u in seen or seen.add(u))][:25]


def make_sheet(rows):
    thumb, label_h, cols, gap = 130, 28, 10, 6
    nrows = (len(rows) + cols - 1) // cols
    canvas = Image.new("RGB", (gap + cols * (thumb + gap), gap + nrows * (thumb + label_h + gap)), "white")
    draw, font = ImageDraw.Draw(canvas), ImageFont.load_default()
    for i, row in enumerate(rows):
        x = gap + (i % cols) * (thumb + gap)
        y = gap + (i // cols) * (thumb + label_h + gap)
        try:
            with Image.open(PHOTOS / row["filename"]) as im:
                canvas.paste(ImageOps.fit(im.convert("RGB"), (thumb, thumb), method=Image.Resampling.LANCZOS), (x, y))
            label = f"#{i+1:03d} {row['engine'][:3]} q{row['query_id']} r{row['rank']}"
            draw.text((x + 2, y + thumb + 3), label, fill="black", font=font)
        except OSError:
            pass
    canvas.save(OUT / "contact_sheet.jpg", "JPEG", quality=88, optimize=True)


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    PHOTOS.mkdir(parents=True)
    s = requests.Session()
    s.headers.update({"User-Agent": random.choice(UA), "Accept-Language": "en-US,en;q=0.9"})

    rows = []
    exact_hashes = set()
    phashes = []
    seen_candidate_urls = set()
    accepted_pages = []

    def accept_image(im, raw, c, used_url, origin):
        sha = hashlib.sha256(raw).hexdigest()
        if sha in exact_hashes:
            return False
        ph = imagehash.phash(im)
        if any((ph - old) <= 2 for old in phashes):
            return False
        idx = len(rows) + 1
        fn = f"anna_claire_clouds_pov_{idx:03d}.jpg"
        im.save(PHOTOS / fn, "JPEG", quality=92, optimize=True, progressive=True)
        exact_hashes.add(sha)
        phashes.append(ph)
        qid = QUERIES.index(c.query) + 1
        rows.append({
            "index": idx, "filename": fn, "engine": c.engine, "query_id": qid,
            "query": c.query, "rank": c.rank, "origin": origin, "title": c.title,
            "source_page": c.source_page, "downloaded_url": used_url,
        })
        print(f"accepted {idx}/{TARGET}: {c.engine} q{qid} r{c.rank} {c.title[:70]}", flush=True)
        return True

    for query in QUERIES:
        if len(rows) >= TARGET:
            break
        candidates = list(ddg_candidates(s, query))
        if len(candidates) < 12:
            candidates.extend(list(bing_candidates(s, query)))
        for c in candidates:
            if len(rows) >= TARGET:
                break
            key = c.thumbnail_url or c.image_url
            if not key or key in seen_candidate_urls:
                continue
            seen_candidate_urls.add(key)
            got = fetch_image(s, [c.thumbnail_url, c.image_url], c.source_page)
            if not got:
                continue
            im, raw, used_url = got
            if accept_image(im, raw, c, used_url, "search_thumbnail"):
                accepted_pages.append(c)

    # Expand only pages that already passed exact relevance; useful for posters and alternate stills.
    if len(rows) < TARGET:
        for c in accepted_pages:
            if len(rows) >= TARGET:
                break
            for u in page_images(s, c):
                if len(rows) >= TARGET:
                    break
                got = fetch_image(s, [u], c.source_page)
                if not got:
                    continue
                im, raw, used_url = got
                accept_image(im, raw, c, used_url, "source_page_image")

    fields = ["index", "filename", "engine", "query_id", "query", "rank", "origin", "title", "source_page", "downloaded_url"]
    with (OUT / "manifest.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader(); w.writerows(rows)
    make_sheet(rows)
    (OUT / "README.txt").write_text(
        f"Fresh search-engine harvest. Collected: {len(rows)} images.\n"
        "Every file comes from a new exact-query POV/VR search or an image embedded on a source page that passed the same relevance lock.\n"
        "manifest.csv records engine, exact query, result rank and source URL.\n",
        encoding="utf-8",
    )
    shutil.make_archive("anna_claire_clouds_fresh_pov_300", "zip", OUT)
    Path("FRESH_POV_COUNT.txt").write_text(str(len(rows)), encoding="utf-8")
    print(f"FINAL_COUNT={len(rows)}", flush=True)

if __name__ == "__main__":
    main()
