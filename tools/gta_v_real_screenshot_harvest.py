from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import shutil
import time
import zipfile
from collections import deque
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

import cv2
import imagehash
import numpy as np
import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont

TARGET = int(os.environ.get("TARGET", "100"))
ROOT = Path("GTA_V_REAL_100")
IMAGES = ROOT / "images"
RAW = ROOT / "_raw"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
CATEGORY_URLS = [
    ("Official Screenshots", "https://www.igrandtheftauto.com/gta5/images/screenshots"),
    ("Gameplay Screenshots", "https://www.igrandtheftauto.com/gta5/images/gameplay"),
]
BAD_TITLE = re.compile(
    r"(?:monkey\s+mosaics|under\s+the\s+bridge|murder\s+mystery|letter\s+scrap|"
    r"spaceship\s+part|nuclear\s+waste|stunt\s+jump|peyote|collectibles?\s+map|"
    r"official\s+logo|cover\s+art|artwork|avatar|t-?shirt|package|scan|roadmap|"
    r"wallpaper|poster|render|box\s*art|game\s+informer)",
    re.I,
)
session = requests.Session()
session.headers.update(HEADERS)


def clean_url(url: str) -> str:
    p = urlparse(url)
    return urlunparse((p.scheme or "https", p.netloc, p.path, "", p.query, ""))


def get(url: str, *, referer: str | None = None, tries: int = 3) -> requests.Response | None:
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer
    for attempt in range(tries):
        try:
            r = session.get(url, headers=headers, timeout=35, allow_redirects=True)
            if r.status_code == 200 and len(r.content) > 500:
                return r
            print(f"HTTP {r.status_code} {url}", flush=True)
        except requests.RequestException as exc:
            print(f"GET_FAIL {type(exc).__name__} {url}", flush=True)
        time.sleep(1.5 * (attempt + 1))
    return None


def title_from_anchor(a: Any) -> str:
    text = " ".join(a.get_text(" ", strip=True).split())
    if text and text.casefold() not in {"grand theft auto v", "gta v", "view", "download"}:
        return text
    img = a.find("img")
    if img:
        alt = " ".join((img.get("alt") or img.get("title") or "").split())
        if alt.casefold() not in {"grand theft auto v", "gta v"}:
            return alt
    return ""


def extract_detail_links(html: str, page_url: str, category: str) -> tuple[list[dict[str, str]], list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, dict[str, str]] = {}
    page_links: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = urljoin(page_url, a["href"])
        path = urlparse(href).path.rstrip("/")
        m = re.search(r"/gta5/images/(\d+)(?:[-/]|$)", path, re.I)
        if m:
            title = title_from_anchor(a)
            parent = a
            for _ in range(6):
                if title:
                    break
                parent = getattr(parent, "parent", None)
                if parent is None:
                    break
                texts = [title_from_anchor(x) for x in parent.find_all("a", href=True)]
                texts = [x for x in texts if x]
                headings = [" ".join(x.get_text(" ", strip=True).split()) for x in parent.find_all(["h2", "h3", "h4", "h5"])]
                headings = [x for x in headings if x and x.casefold() not in {"grand theft auto v", "gta v"}]
                choices = texts + headings
                if choices:
                    title = max(choices, key=len)
            detail = clean_url(href)
            candidate = {"detail_url": detail, "title": title or f"GTA V Screenshot {m.group(1)}", "category": category}
            old = found.get(detail)
            if old is None or old["title"].startswith("GTA V Screenshot"):
                found[detail] = candidate
            continue
        low = (a.get_text(" ", strip=True) or "").lower()
        if ("next" in low or a.get("rel") == ["next"] or "page=" in href or "/page/" in href) and "/gta5/images" in href:
            page_links.add(clean_url(href))
    print(f"LIST {page_url} cards={len(found)} pages={len(page_links)}", flush=True)
    return list(found.values()), sorted(page_links)


def discover_candidates() -> list[dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for category, start in CATEGORY_URLS:
        queue: deque[str] = deque([start])
        for page in range(2, 9):
            queue.extend([
                f"{start}?page={page}",
                f"{start}/page/{page}",
                f"{start}/{page}",
                f"{start}?p={page}",
            ])
        seen_pages: set[str] = set()
        while queue and len(seen_pages) < 36:
            page_url = queue.popleft()
            if page_url in seen_pages:
                continue
            seen_pages.add(page_url)
            r = get(page_url)
            if not r:
                continue
            cards, more = extract_detail_links(r.text, page_url, category)
            for card in cards:
                if not BAD_TITLE.search(card["title"]):
                    out[card["detail_url"]] = card
            for nxt in more:
                if nxt not in seen_pages:
                    queue.append(nxt)
    rows = list(out.values())
    rows.sort(key=lambda x: (0 if x["category"] == "Official Screenshots" else 1, x["title"].casefold()))
    return rows


def image_urls_from_detail(html: str, detail_url: str, screenshot_id: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    urls: list[str] = []
    for key, value in [("property", "og:image"), ("name", "twitter:image")]:
        tag = soup.find("meta", attrs={key: value})
        if tag and tag.get("content"):
            urls.append(urljoin(detail_url, tag["content"]))
    for tag in soup.find_all(["img", "a", "source"]):
        for attr in ("src", "data-src", "data-original", "href", "srcset"):
            raw = tag.get(attr)
            if not raw:
                continue
            for part in str(raw).split(","):
                u = urljoin(detail_url, part.strip().split(" ")[0])
                if "/content/images/" in u and (screenshot_id in urlparse(u).path or not screenshot_id):
                    urls.append(u)
    unique: list[str] = []
    seen: set[str] = set()
    for u in urls:
        u = clean_url(u)
        if u in seen:
            continue
        seen.add(u)
        low = u.lower()
        if any(x in low for x in ("logo", "avatar", "icon", "thumb")):
            continue
        unique.append(u)
    unique.sort(key=lambda u: (0 if "/content/images/" in u else 1, -len(u)))
    return unique


def normalized_group(title: str) -> str:
    t = title.casefold()
    t = re.sub(r"\b(?:gta|grand theft auto|v|5)\b", " ", t)
    t = re.sub(r"\b\d+\b", " ", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    words = [w for w in t.split() if w not in {"the", "a", "an", "in", "at", "on", "with", "and", "of", "to", "from", "for"}]
    return " ".join(words[:8])


def metrics(image_bytes: bytes) -> tuple[Image.Image, dict[str, Any]] | None:
    try:
        im = Image.open(BytesIO(image_bytes)).convert("RGB")
    except Exception:
        return None
    w, h = im.size
    ratio = w / max(h, 1)
    if w < 760 or h < 420 or not (1.28 <= ratio <= 2.45):
        return None
    arr = cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2BGR)
    gray = cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY)
    brightness = float(gray.mean())
    contrast = float(gray.std())
    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    if brightness < 10 or brightness > 247 or contrast < 14 or sharpness < 10:
        return None
    if float((gray < 8).mean()) > 0.62 or float((gray > 250).mean()) > 0.62:
        return None
    hsv = cv2.cvtColor(arr, cv2.COLOR_BGR2HSV)
    small = cv2.resize(hsv, (192, 108), interpolation=cv2.INTER_AREA)
    hist = cv2.calcHist([small], [0, 1], None, [24, 12], [0, 180, 0, 256]).flatten().astype(np.float32)
    hist /= max(float(np.linalg.norm(hist)), 1e-8)
    ph = imagehash.phash(im, hash_size=16)
    quality = math.log1p(w * h) + 0.7 * math.log1p(sharpness) + 0.012 * contrast
    return im, {
        "width": w,
        "height": h,
        "brightness": brightness,
        "contrast": contrast,
        "sharpness": sharpness,
        "hist": hist,
        "phash": ph,
        "quality": quality,
    }


def download_candidates(cards: list[dict[str, str]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    groups: set[str] = set()
    for pos, card in enumerate(cards, 1):
        title = card["title"].strip()
        group = normalized_group(title)
        if not group or group in groups or BAD_TITLE.search(title):
            continue
        r = get(card["detail_url"])
        if not r:
            continue
        sid_match = re.search(r"/gta5/images/(\d+)", card["detail_url"])
        sid = sid_match.group(1) if sid_match else ""
        page_text = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)
        if card["category"] not in page_text and not any(x in page_text for x in ("Official Screenshots", "Gameplay Screenshots")):
            continue
        for image_url in image_urls_from_detail(r.text, card["detail_url"], sid):
            ir = get(image_url, referer=card["detail_url"], tries=2)
            if not ir or "image" not in (ir.headers.get("content-type") or "").lower():
                continue
            result = metrics(ir.content)
            if not result:
                continue
            im, met = result
            raw_path = RAW / f"candidate_{len(rows)+1:03d}.jpg"
            im.save(raw_path, "JPEG", quality=96, optimize=True)
            row = {
                **card,
                **met,
                "group": group,
                "image_url": image_url,
                "raw_path": raw_path,
                "sha256": hashlib.sha256(ir.content).hexdigest(),
            }
            duplicate = False
            for kept in rows:
                hamming = row["phash"] - kept["phash"]
                hist_sim = float(np.dot(row["hist"], kept["hist"]))
                if hamming <= 16 or (hamming <= 27 and hist_sim >= 0.965):
                    duplicate = True
                    break
            if not duplicate:
                rows.append(row)
                groups.add(group)
            else:
                raw_path.unlink(missing_ok=True)
            break
        print(f"DETAIL {pos}/{len(cards)} candidates={len(rows)} title={title[:70]}", flush=True)
        if len(rows) >= max(TARGET + 60, 160):
            break
    return rows


def choose_diverse(rows: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    if len(rows) <= count:
        return rows
    q = np.array([x["quality"] for x in rows], dtype=np.float32)
    q = (q - q.min()) / max(float(q.max() - q.min()), 1e-8)
    for row, score in zip(rows, q.tolist()):
        row["qnorm"] = score
    chosen: list[dict[str, Any]] = [max(rows, key=lambda x: x["qnorm"])]
    remaining = [x for x in rows if x is not chosen[0]]
    while remaining and len(chosen) < count:
        def score(c: dict[str, Any]) -> float:
            distances = []
            for p in chosen:
                hd = (c["phash"] - p["phash"]) / 256.0
                hist_d = max(0.0, 1.0 - float(np.dot(c["hist"], p["hist"])))
                distances.append(0.64 * hd + 0.36 * hist_d)
            category_bonus = 0.015 if c["category"] == "Official Screenshots" else 0.0
            return 0.82 * min(distances) + 0.18 * c["qnorm"] + category_bonus
        best = max(remaining, key=score)
        chosen.append(best)
        remaining.remove(best)
    return chosen


def slugify(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()
    return s[:70] or "gta_v_gameplay"


def save_selected(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    manifest: list[dict[str, Any]] = []
    for i, row in enumerate(rows, 1):
        src = Path(row["raw_path"])
        with Image.open(src) as im:
            im = im.convert("RGB")
            if im.width > 2560:
                nh = round(im.height * 2560 / im.width)
                im = im.resize((2560, nh), Image.Resampling.LANCZOS)
            filename = f"{i:03d}_{slugify(row['title'])}.jpg"
            dst = IMAGES / filename
            im.save(dst, "JPEG", quality=95, optimize=True)
        manifest.append({
            "index": i,
            "filename": filename,
            "title": row["title"],
            "category": row["category"],
            "detail_page": row["detail_url"],
            "source_image": row["image_url"],
            "source_width": row["width"],
            "source_height": row["height"],
            "sha256": row["sha256"],
            "phash": str(row["phash"]),
        })
    return manifest


def font(size: int) -> ImageFont.ImageFont:
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"):
        if Path(p).exists():
            return ImageFont.truetype(p, size=size)
    return ImageFont.load_default()


def contact_sheet(manifest: list[dict[str, Any]]) -> None:
    cols, cell_w, image_h, label_h, header = 5, 360, 205, 58, 78
    rows = math.ceil(len(manifest) / cols)
    canvas = Image.new("RGB", (cols * cell_w, header + rows * (image_h + label_h)), "#0d0d0d")
    draw = ImageDraw.Draw(canvas)
    draw.text((20, 18), "GTA V — 100 genuinely different gameplay scenes", fill="white", font=font(27))
    draw.text((20, 51), "Public screenshot galleries · no video-frame bursts · no FiveM menu captures", fill="#bdbdbd", font=font(14))
    for idx, item in enumerate(manifest):
        rr, cc = divmod(idx, cols)
        x, y = cc * cell_w, header + rr * (image_h + label_h)
        im = Image.open(IMAGES / item["filename"]).convert("RGB")
        im.thumbnail((cell_w, image_h), Image.Resampling.LANCZOS)
        frame = Image.new("RGB", (cell_w, image_h), "black")
        frame.paste(im, ((cell_w - im.width) // 2, (image_h - im.height) // 2))
        canvas.paste(frame, (x, y))
        draw.rectangle((x, y + image_h, x + cell_w, y + image_h + label_h), fill="#1a1a1a")
        title = item["title"]
        if len(title) > 43:
            title = title[:40] + "..."
        draw.text((x + 7, y + image_h + 7), f"{item['index']:03d} · {title}", fill="white", font=font(12))
        draw.text((x + 7, y + image_h + 31), item["category"], fill="#a7a7a7", font=font(11))
    canvas.save(ROOT / "CONTACT_SHEET_100.jpg", "JPEG", quality=91, optimize=True)


def write_outputs(manifest: list[dict[str, Any]], discovered: int, accepted: int) -> None:
    (ROOT / "sources.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    if manifest:
        with (ROOT / "sources.csv").open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=list(manifest[0].keys()))
            writer.writeheader()
            writer.writerows(manifest)
    (ROOT / "README.txt").write_text(
        "GTA V — 100 REAL GAMEPLAY SCREENSHOTS\n\n"
        "Rules applied:\n"
        "- one source screenshot per file; no extraction of repeated frames from a video or GIF\n"
        "- only Official Screenshots / Gameplay Screenshots gallery categories\n"
        "- collectible series, artwork and UI-only captures excluded\n"
        "- exact and perceptual duplicates rejected\n"
        "- visual-diversity selection over an oversupply of candidates\n"
        "- originals kept uncropped and with their native aspect ratio\n\n"
        "See sources.csv or sources.json for the source page and direct image URL of every file.\n",
        encoding="utf-8",
    )
    result = {
        "requested": TARGET,
        "selected": len(manifest),
        "discovered_detail_pages": discovered,
        "accepted_unique_candidates": accepted,
        "status": "complete" if len(manifest) == TARGET else "partial",
    }
    Path("GTA_V_REAL_100_RESULT.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    with zipfile.ZipFile("GTA_V_REAL_100_SCREENSHOTS.zip", "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in sorted(ROOT.rglob("*")):
            if p.is_file() and "_raw" not in p.parts:
                z.write(p, p.relative_to(ROOT.parent))


def main() -> None:
    shutil.rmtree(ROOT, ignore_errors=True)
    IMAGES.mkdir(parents=True, exist_ok=True)
    RAW.mkdir(parents=True, exist_ok=True)
    cards = discover_candidates()
    print(f"DISCOVERED {len(cards)} eligible screenshot pages", flush=True)
    candidates = download_candidates(cards)
    print(f"ACCEPTED {len(candidates)} unique high-resolution candidates", flush=True)
    selected = choose_diverse(candidates, TARGET)
    manifest = save_selected(selected)
    if manifest:
        contact_sheet(manifest)
    write_outputs(manifest, len(cards), len(candidates))
    if len(manifest) != TARGET:
        raise SystemExit(f"Expected {TARGET} images, produced {len(manifest)}")


if __name__ == "__main__":
    main()
