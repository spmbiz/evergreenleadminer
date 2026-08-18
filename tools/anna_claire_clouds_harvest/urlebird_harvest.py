#!/usr/bin/env python3
from __future__ import annotations

import csv
import hashlib
import json
import os
import random
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin

import imagehash
import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

TARGET = int(os.getenv("TARGET_COUNT", "200"))
ROOT = Path(__file__).resolve().parent
OUT = ROOT / "output"
PHOTOS = OUT / "photos"
LOGS = ROOT / "logs"
USER_URL = "https://urlebird.com/user/annaclaireclouds/"
AJAX_URL = "https://urlebird.com/ajax/"

UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]


@dataclass(frozen=True)
class Entry:
    video_page: str
    thumb_fallback: str
    caption_fallback: str


def headers(referer: str | None = None) -> dict[str, str]:
    h = {
        "User-Agent": random.choice(UAS),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    }
    if referer:
        h["Referer"] = referer
    return h


def get_retry(session: requests.Session, url: str, *, referer: str | None = None, stream: bool = False):
    last = None
    for attempt in range(4):
        try:
            r = session.get(url, headers=headers(referer), timeout=25, allow_redirects=True, stream=stream)
            if r.status_code == 200:
                return r
            last = RuntimeError(f"HTTP {r.status_code} for {url}")
        except requests.RequestException as exc:
            last = exc
        time.sleep(0.8 * (attempt + 1))
    raise last or RuntimeError(f"Unable to fetch {url}")


def parse_entries(fragment: str) -> list[Entry]:
    soup = BeautifulSoup(fragment, "html.parser")
    out: list[Entry] = []
    for thumb in soup.select("#thumbs div.thumb, div.thumb"):
        link = thumb.select_one("div.info3 a[href]") or thumb.select_one("a[href]")
        if not link:
            continue
        href = urljoin("https://urlebird.com", link.get("href", ""))
        if "/video/" not in href:
            continue
        img = thumb.select_one("img")
        img_url = ""
        if img:
            for attr in ("data-src", "data-original", "src"):
                value = img.get(attr)
                if value and str(value).startswith("http"):
                    img_url = str(value)
                    break
        caption = link.get_text(" ", strip=True)
        out.append(Entry(href, img_url, caption))
    return out


def collect_video_entries() -> list[Entry]:
    LOGS.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update(headers())
    r = get_retry(session, USER_URL)
    html = r.text
    (LOGS / "urlebird_page1.html").write_text(html, encoding="utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")
    entries = parse_entries(html)
    seen = {e.video_page for e in entries}

    btn = soup.select_one("#load_more")
    if not btn:
        print("No load-more button found; initial entries:", len(entries))
        return entries

    state = {
        "user_id": btn.get("data-user-id") or "",
        "sec_uid": btn.get("data-sec-uid") or "",
        "cursor": btn.get("data-cursor") or "",
        "lang": btn.get("data-lang") or "en",
        "page": "2",
        "x": btn.get("data-x") or "",
    }

    for page in range(2, 35):
        if len(entries) >= max(TARGET + 100, 320):
            break
        payload = {"action": "user", "data": json.dumps(state, separators=(",", ":"))}
        try:
            resp = session.post(AJAX_URL, data=payload, headers={**headers(USER_URL), "X-Requested-With": "XMLHttpRequest"}, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            print(f"AJAX page {page} failed: {exc}", file=sys.stderr)
            break
        thumbs = data.get("thumbs") or ""
        if not thumbs:
            print("No more thumbnails at page", page)
            break
        fragment = f'<div id="thumbs">{thumbs}</div>'
        fresh = 0
        for entry in parse_entries(fragment):
            if entry.video_page not in seen:
                entries.append(entry)
                seen.add(entry.video_page)
                fresh += 1
        print(f"PAGE {page}: fresh={fresh}, total={len(entries)}")
        if fresh == 0:
            break
        if data.get("u") is not None:
            state["user_id"] = str(data["u"])
        if data.get("s") is not None:
            state["sec_uid"] = str(data["s"])
        if data.get("cursor") is not None:
            state["cursor"] = str(data["cursor"])
        if data.get("x") is not None:
            state["x"] = str(data["x"])
        state["page"] = str(page + 1)
        time.sleep(0.5)

    (LOGS / "video_pages.txt").write_text("\n".join(e.video_page for e in entries), encoding="utf-8")
    return entries


def normalize_thumb(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item.startswith("http"):
                return item
    if isinstance(value, dict):
        for key in ("url", "contentUrl"):
            item = value.get(key)
            if isinstance(item, str):
                return item
    return ""


def fetch_metadata(entry: Entry) -> dict[str, str]:
    session = requests.Session()
    try:
        r = get_retry(session, entry.video_page)
        soup = BeautifulSoup(r.text, "html.parser")
        script = soup.select_one("#VideoObject")
        if script:
            raw = script.string or script.get_text(strip=True)
            info = json.loads(raw)
            return {
                "video_page": entry.video_page,
                "thumbnail_url": normalize_thumb(info.get("thumbnailURL")) or entry.thumb_fallback,
                "source_url": str(info.get("url") or entry.video_page),
                "caption": str(info.get("name") or entry.caption_fallback),
                "upload_date": str(info.get("uploadDate") or ""),
            }
    except Exception as exc:
        return {
            "video_page": entry.video_page,
            "thumbnail_url": entry.thumb_fallback,
            "source_url": entry.video_page,
            "caption": entry.caption_fallback,
            "upload_date": "",
            "error": str(exc),
        }
    return {
        "video_page": entry.video_page,
        "thumbnail_url": entry.thumb_fallback,
        "source_url": entry.video_page,
        "caption": entry.caption_fallback,
        "upload_date": "",
    }


def fetch_image(meta: dict[str, str]):
    url = meta.get("thumbnail_url", "")
    if not url.startswith("http"):
        return None
    session = requests.Session()
    try:
        r = get_retry(session, url, referer=meta.get("video_page"), stream=True)
        content_type = (r.headers.get("content-type") or "").lower()
        if "html" in content_type or "svg" in content_type:
            return None
        buf = bytearray()
        for chunk in r.iter_content(65536):
            if chunk:
                buf.extend(chunk)
                if len(buf) > 18 * 1024 * 1024:
                    return None
        with Image.open(BytesIO(buf)) as im:
            im.load()
            im = ImageOps.exif_transpose(im)
            if min(im.size) < 260:
                return None
            if max(im.width / im.height, im.height / im.width) > 3.6:
                return None
            return meta, im.convert("RGB").copy(), bytes(buf)
    except (requests.RequestException, OSError, UnidentifiedImageError, ValueError):
        return None


def make_sheet(rows: list[dict[str, str]]) -> None:
    thumb, gap, label, cols = 150, 8, 22, 10
    nrows = (len(rows) + cols - 1) // cols
    sheet = Image.new("RGB", (gap + cols * (thumb + gap), gap + nrows * (thumb + label + gap)), "white")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    for i, row in enumerate(rows):
        x = gap + (i % cols) * (thumb + gap)
        y = gap + (i // cols) * (thumb + label + gap)
        with Image.open(PHOTOS / row["filename"]) as im:
            tile = ImageOps.fit(im.convert("RGB"), (thumb, thumb), method=Image.Resampling.LANCZOS)
        sheet.paste(tile, (x, y))
        draw.text((x + 3, y + thumb + 3), f"#{i + 1:03d}", fill="black", font=font)
    sheet.save(OUT / "contact_sheet.jpg", "JPEG", quality=90, optimize=True)


def main() -> int:
    for p in (OUT, LOGS):
        if p.exists():
            shutil.rmtree(p)
    PHOTOS.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)

    entries = collect_video_entries()
    print("VIDEO_ENTRIES", len(entries))
    if not entries:
        (ROOT / "COLLECTION_COUNT.txt").write_text("0", encoding="utf-8")
        return 2

    metadata: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(fetch_metadata, e) for e in entries[:360]]
        for n, future in enumerate(as_completed(futures), 1):
            item = future.result()
            if item.get("thumbnail_url"):
                metadata.append(item)
            if n % 25 == 0:
                print("METADATA", n, "valid", len(metadata))

    (LOGS / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    results = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(fetch_image, m) for m in metadata]
        for n, future in enumerate(as_completed(futures), 1):
            item = future.result()
            if item:
                results.append(item)
            if n % 25 == 0:
                print("IMAGES", n, "downloaded", len(results))

    # Keep the source page order, not completion order.
    order = {e.video_page: i for i, e in enumerate(entries)}
    results.sort(key=lambda item: order.get(item[0].get("video_page", ""), 999999))

    sha_seen: set[str] = set()
    phashes: list[imagehash.ImageHash] = []
    rows: list[dict[str, str]] = []
    for meta, image, raw in results:
        if len(rows) >= TARGET:
            break
        sha = hashlib.sha256(raw).hexdigest()
        if sha in sha_seen:
            continue
        ph = imagehash.phash(image)
        if any((ph - old) <= 1 for old in phashes):
            continue
        idx = len(rows) + 1
        filename = f"anna_claire_clouds_{idx:03d}.jpg"
        image.save(PHOTOS / filename, "JPEG", quality=94, optimize=True, progressive=True)
        sha_seen.add(sha)
        phashes.append(ph)
        rows.append({
            "index": str(idx),
            "filename": filename,
            "source": "TikTok public thumbnail via Urlebird",
            "source_url": meta.get("source_url", ""),
            "mirror_page": meta.get("video_page", ""),
            "thumbnail_url": meta.get("thumbnail_url", ""),
            "caption": meta.get("caption", ""),
            "upload_date": meta.get("upload_date", ""),
            "width": str(image.width),
            "height": str(image.height),
            "sha256_original": sha,
        })
        print(f"ACCEPT {idx}/{TARGET} {image.width}x{image.height}")

    fields = ["index", "filename", "source", "source_url", "mirror_page", "thumbnail_url", "caption", "upload_date", "width", "height", "sha256_original"]
    with (OUT / "manifest.csv").open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    if rows:
        make_sheet(rows)
    (OUT / "README.txt").write_text(
        "Anna Claire Clouds — 200 public TikTok reference stills\n"
        f"Collected: {len(rows)} / {TARGET}\n\n"
        "Source account: official TikTok @annaclaireclouds, indexed by the public Urlebird mirror.\n"
        "Each image is a public video thumbnail/still. Corrupt files, low-resolution files, exact duplicates, and near-exact duplicates were removed.\n"
        "A source manifest and visual contact sheet are included. Rights remain with their owners; verify permission before publication or commercial use.\n",
        encoding="utf-8",
    )
    shutil.make_archive("anna_claire_clouds_200_public_tiktok_stills", "zip", OUT)
    (ROOT / "COLLECTION_COUNT.txt").write_text(str(len(rows)), encoding="utf-8")
    print("FINAL_COUNT", len(rows))
    return 0 if len(rows) >= TARGET else 2


if __name__ == "__main__":
    raise SystemExit(main())
