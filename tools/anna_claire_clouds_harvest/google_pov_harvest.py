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
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

import imagehash
import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

TARGET = int(os.getenv("TARGET_COUNT", "200"))
OUT = Path("google_pov_output")
PHOTOS = OUT / "photos"
TIMEOUT = 14
MIN_W = 180
MIN_H = 140
MAX_BYTES = 18 * 1024 * 1024

QUERIES = [
    '"Anna Claire Clouds" POV',
    '"Anna Claire Clouds" POV scene',
    '"Anna Claire Clouds" POV video',
    '"Anna Claire Clouds" POV preview',
    '"Anna Claire Clouds" POV trailer',
    '"Anna Claire Clouds" POV thumbnail',
    '"Anna Claire Clouds" first person scene',
    '"Anna Claire Clouds" VR POV',
    '"Anna Claire Clouds" POVR',
    '"Anna Claire Clouds" "Happy Little Clouds"',
    '"Anna Claire Clouds" "POV Hookups"',
    '"Anna Claire Clouds" "Mr. Big POV"',
    '"Anna Claire Clouds" scene preview',
    '"Anna Claire Clouds" video still',
    '"Anna Claire Clouds" movie still',
    '"Anna Claire Clouds" trailer screenshot',
    '"Anna Claire Clouds" site:avn.com POV',
    '"Anna Claire Clouds" site:xbiz.com POV',
    '"Anna Claire Clouds" site:adulttheculture.com POV',
    '"Anna Claire Clouds" site:youtube.com POV',
    '"Anna Claire Clouds" site:imdb.com video',
]

BLOCKED_TEXT = {
    "gangbang", "bukkake", "cumshot", "blowjob", "anal-sex", "anal sex",
    "hardcore", "full scene", "full-scene", "leak", "torrent", "rule34",
}

SKIP_HOSTS = {
    "google.com", "www.google.com", "accounts.google.com", "support.google.com",
    "bing.com", "www.bing.com", "microsoft.com", "gstatic.com",
}

UA = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/17 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/125 Safari/537.36",
]


@dataclass(frozen=True)
class Candidate:
    image_url: str
    source_url: str
    query: str
    origin: str
    title: str = ""


def host(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().split(":")[0].removeprefix("www.")
    except Exception:
        return ""


def blocked(c: Candidate) -> bool:
    text = html.unescape(f"{c.title} {c.source_url} {c.image_url}").lower()
    return any(term in text for term in BLOCKED_TEXT)


def clean_url(value: str) -> str:
    value = html.unescape(value).replace("\\u003d", "=").replace("\\u0026", "&")
    value = value.replace("\\/", "/")
    value = value.strip('"\' ,')
    if value.startswith("//"):
        value = "https:" + value
    return value


def google_redirect(href: str) -> str:
    if not href:
        return ""
    if href.startswith("/url?"):
        qs = parse_qs(urlparse(href).query)
        return qs.get("q", qs.get("url", [""]))[0]
    if href.startswith("http"):
        return href
    return ""


def google_image_candidates(s: requests.Session, query: str):
    params = {"tbm": "isch", "q": query, "safe": "off", "hl": "en", "ijn": "0"}
    try:
        r = s.get("https://www.google.com/search", params=params, timeout=TIMEOUT)
        r.raise_for_status()
    except requests.RequestException as exc:
        print(f"[google images error] {query}: {exc}", flush=True)
        return
    text = r.text
    soup = BeautifulSoup(text, "html.parser")
    seen: set[str] = set()

    for img in soup.find_all("img"):
        src = clean_url(img.get("data-src") or img.get("src") or "")
        if not src.startswith("http") or src in seen:
            continue
        seen.add(src)
        parent = img.find_parent("a")
        source = google_redirect(parent.get("href", "")) if parent else ""
        title = img.get("alt") or ""
        yield Candidate(src, source, query, "google_thumbnail", title)

    patterns = [
        r'"ou":"(https?://[^" ]+)"',
        r'\["(https?://[^" ]+\.(?:jpg|jpeg|png|webp)(?:\?[^" ]*)?)",\d+,\d+\]',
        r'(https?://[^"\\ ]+\.(?:jpg|jpeg|png|webp)(?:\?[^"\\ ]*)?)',
    ]
    for pat in patterns:
        for raw in re.findall(pat, text, flags=re.I):
            url = clean_url(raw)
            if not url.startswith("http") or url in seen:
                continue
            seen.add(url)
            yield Candidate(url, "", query, "google_original_regex", "")


def google_web_pages(s: requests.Session, query: str, limit: int = 12):
    params = {"q": query, "num": "30", "safe": "off", "hl": "en"}
    try:
        r = s.get("https://www.google.com/search", params=params, timeout=TIMEOUT)
        r.raise_for_status()
    except requests.RequestException:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    pages: list[str] = []
    for a in soup.find_all("a", href=True):
        u = google_redirect(a["href"])
        h = host(u)
        if not u.startswith("http") or not h or h in SKIP_HOSTS or h.endswith("google.com"):
            continue
        if u not in pages:
            pages.append(u)
        if len(pages) >= limit:
            break
    return pages


def page_candidates(s: requests.Session, page_url: str, query: str):
    try:
        r = s.get(page_url, timeout=TIMEOUT, allow_redirects=True)
        r.raise_for_status()
    except requests.RequestException:
        return
    if "text/html" not in (r.headers.get("content-type") or ""):
        return
    soup = BeautifulSoup(r.text, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    page_text = f"{title} {page_url}".lower()
    if "anna claire clouds" not in page_text and "annaclaireclouds" not in re.sub(r"[^a-z]", "", page_text):
        # Exact query lock remains the primary filter; page title lock prevents generic result pages.
        return
    attrs = [
        ("meta", {"property": "og:image"}, "content", "page_og_image"),
        ("meta", {"name": "twitter:image"}, "content", "page_twitter_image"),
        ("meta", {"name": "twitter:image:src"}, "content", "page_twitter_image"),
        ("video", {}, "poster", "video_poster"),
    ]
    seen = set()
    for tagname, selector, attr, origin in attrs:
        for tag in soup.find_all(tagname, attrs=selector):
            u = clean_url(tag.get(attr) or "")
            u = urljoin(r.url, u)
            if u.startswith("http") and u not in seen:
                seen.add(u)
                yield Candidate(u, r.url, query, origin, title)
    for img in soup.find_all("img"):
        u = clean_url(img.get("data-src") or img.get("data-lazy-src") or img.get("src") or "")
        u = urljoin(r.url, u)
        if not u.startswith("http") or u in seen:
            continue
        alt = img.get("alt") or ""
        combined = f"{alt} {img.get('title') or ''}".lower()
        if any(k in combined for k in ("anna", "cloud", "pov", "preview", "trailer", "scene", "video")):
            seen.add(u)
            yield Candidate(u, r.url, query, "page_inline_image", f"{title} | {alt}")


def bing_candidates(s: requests.Session, query: str, pages: int = 4):
    seen = set()
    for page in range(pages):
        first = page * 35 + 1
        url = f"https://www.bing.com/images/async?q={quote_plus(query)}&first={first}&count=35&adlt=off&qft=%2Bfilterui%3Aphoto-photo"
        try:
            r = s.get(url, timeout=TIMEOUT)
            r.raise_for_status()
        except requests.RequestException:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup.select("a.iusc[m]"):
            try:
                m = json.loads(tag.get("m") or "{}")
            except Exception:
                continue
            img = clean_url(str(m.get("murl") or ""))
            src = clean_url(str(m.get("purl") or ""))
            title = str(m.get("t") or "")
            if not img.startswith("http") or img in seen:
                continue
            seen.add(img)
            yield Candidate(img, src, query, "bing_exact_query_fallback", title)
        time.sleep(0.15)


def fetch_image(s: requests.Session, c: Candidate):
    headers = {
        "User-Agent": random.choice(UA),
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    if c.source_url.startswith("http"):
        headers["Referer"] = c.source_url
    try:
        with s.get(c.image_url, headers=headers, timeout=TIMEOUT, stream=True, allow_redirects=True) as r:
            r.raise_for_status()
            ctype = (r.headers.get("content-type") or "").lower()
            if "html" in ctype or "svg" in ctype:
                return None
            raw = bytearray()
            for chunk in r.iter_content(65536):
                if not chunk:
                    continue
                raw.extend(chunk)
                if len(raw) > MAX_BYTES:
                    return None
    except requests.RequestException:
        return None
    try:
        with Image.open(BytesIO(raw)) as im:
            im.load()
            im = ImageOps.exif_transpose(im).convert("RGB")
            if im.width < MIN_W or im.height < MIN_H:
                return None
            ratio = max(im.width / im.height, im.height / im.width)
            if ratio > 4.5:
                return None
            return im.copy(), bytes(raw)
    except (UnidentifiedImageError, OSError, ValueError):
        return None


def make_sheet(rows: list[dict[str, str]]):
    size, label_h, cols, gap = 170, 32, 8, 7
    nrows = (len(rows) + cols - 1) // cols
    canvas = Image.new("RGB", (gap + cols * (size + gap), gap + nrows * (size + label_h + gap)), "white")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()
    for i, row in enumerate(rows):
        x = gap + (i % cols) * (size + gap)
        y = gap + (i // cols) * (size + label_h + gap)
        try:
            with Image.open(PHOTOS / row["filename"]) as im:
                thumb = ImageOps.fit(im.convert("RGB"), (size, size), method=Image.Resampling.LANCZOS)
                canvas.paste(thumb, (x, y))
        except OSError:
            continue
        draw.text((x + 2, y + size + 2), f"#{i+1:03d} {row['origin'][:18]}", fill="black", font=font)
        draw.text((x + 2, y + size + 15), row["query"].replace('"Anna Claire Clouds" ', "")[:24], fill="black", font=font)
    canvas.save(OUT / "contact_sheet.jpg", quality=88, optimize=True)


def main() -> int:
    if OUT.exists():
        shutil.rmtree(OUT)
    PHOTOS.mkdir(parents=True)
    s = requests.Session()
    s.headers.update({"User-Agent": random.choice(UA), "Accept-Language": "en-US,en;q=0.9"})

    rows: list[dict[str, str]] = []
    exact_hashes: set[str] = set()
    phashes: list[imagehash.ImageHash] = []
    seen_urls: set[str] = set()

    def accept(c: Candidate) -> None:
        if len(rows) >= TARGET or c.image_url in seen_urls or blocked(c):
            return
        seen_urls.add(c.image_url)
        got = fetch_image(s, c)
        if not got:
            return
        im, raw = got
        sha = hashlib.sha256(raw).hexdigest()
        if sha in exact_hashes:
            return
        ph = imagehash.phash(im)
        if any((ph - old) <= 3 for old in phashes):
            return
        idx = len(rows) + 1
        fn = f"anna_claire_clouds_pov_{idx:03d}.jpg"
        im.save(PHOTOS / fn, "JPEG", quality=92, optimize=True, progressive=True)
        exact_hashes.add(sha)
        phashes.append(ph)
        rows.append({
            "index": str(idx),
            "filename": fn,
            "origin": c.origin,
            "query": c.query,
            "title": c.title,
            "source_page": c.source_url,
            "image_url": c.image_url,
            "domain": host(c.source_url) or host(c.image_url),
            "width": str(im.width),
            "height": str(im.height),
        })
        print(f"accepted {idx}: {c.origin} | {rows[-1]['domain']} | {c.query}", flush=True)

    for query in QUERIES:
        if len(rows) >= TARGET:
            break
        print(f"GOOGLE IMAGE QUERY: {query}", flush=True)
        for c in google_image_candidates(s, query) or []:
            accept(c)
            if len(rows) >= TARGET:
                break
        for page_url in google_web_pages(s, query):
            if len(rows) >= TARGET:
                break
            for c in page_candidates(s, page_url, query) or []:
                accept(c)
        time.sleep(0.3)

    if len(rows) < TARGET:
        print(f"Google/direct pages yielded {len(rows)}; running exact-query Bing fallback", flush=True)
        for query in QUERIES:
            if len(rows) >= TARGET:
                break
            for c in bing_candidates(s, query):
                accept(c)
                if len(rows) >= TARGET:
                    break

    fields = ["index", "filename", "origin", "query", "title", "source_page", "image_url", "domain", "width", "height"]
    with (OUT / "manifest.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    make_sheet(rows)
    (OUT / "README.txt").write_text(
        "Exact-query Google Images/web-page harvest focused on POV, video previews, trailers and scene stills for Anna Claire Clouds.\n"
        f"Collected: {len(rows)} unique images.\n"
        "The manifest records every query, source page and direct image URL.\n",
        encoding="utf-8",
    )
    Path("GOOGLE_POV_COUNT.txt").write_text(str(len(rows)), encoding="utf-8")
    shutil.make_archive("anna_claire_clouds_google_pov_stills", "zip", OUT)
    print(f"FINAL_COUNT={len(rows)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
