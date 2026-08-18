#!/usr/bin/env python3
from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import os
import random
import re
import shutil
import time
import unicodedata
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote_plus, urlparse

import imagehash
import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont, ImageOps

try:
    from ddgs import DDGS
except Exception:
    DDGS = None

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36"
MIN_LONG = 700
MAX_LONG = 2600
BATCH_SIZE = 12
BATCH_INDEX = int(os.environ.get("BATCH_INDEX", "0"))
OUT = Path(f"people-photos-next93-batch-{BATCH_INDEX:02d}")
ZIP_PATH = Path(f"people-photos-next93-batch-{BATCH_INDEX:02d}.zip")

BAD_DOMAINS = (
    "pinterest.", "pinimg.", "facebook.", "instagram.", "tiktok.", "gettyimages.",
    "shutterstock.", "alamy.", "dreamstime.", "depositphotos.", "123rf.",
    "wallpaper", "listal.", "fanpop.", "fandom.", "spotify.", "youtube.",
    "youtu.be", "ytimg.", "amazon.", "goodreads.", "soundcloud."
)
GOOD_DOMAINS = (
    "wikimedia.", "wikipedia.", "reuters.", "apnews.", "bbc.", "theguardian.",
    "variety.", "deadline.", "hollywoodreporter.", "rollingstone.", "billboard.",
    "grammy.", "gq.", "vogue.", "esquire.", "forbes.", "complex.", "nba.",
    "ufc.", "oscars.", "festival-cannes.", "bafta.", "sagaftra.", "netflix.",
    "paramount.", "warnerbros.", "sonypictures.", "universalpictures."
)
BAD_TEXT = (
    "logo", "album cover", "book cover", "poster", "wallpaper", "illustration",
    "vector", "drawing", "painting", "meme", "quote", "merch", "t-shirt",
    "hoodie", "funko", "wax figure", "lookalike", "impersonator", "ai generated",
    "deepfake", "thumbnail template", "birthday cake", "action figure"
)
SLOTS = [
    ("portrait-front", ["portrait headshot", "close up face", "official portrait"]),
    ("three-quarter", ["three quarter portrait", "red carpet portrait", "press portrait"]),
    ("profile-side", ["side profile", "speaking side view", "candid profile"]),
    ("full-body", ["full body standing", "full length red carpet", "walking event"]),
    ("context-event", ["event interview stage", "speaking on stage", "professional candid"]),
]


def norm(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def slug(value: str) -> str:
    return norm(value).replace(" ", "-")


def meaningful_tokens(value: str) -> list[str]:
    stop = {
        "american", "british", "canadian", "international", "film", "actor",
        "rapper", "singer", "producer", "internet", "personality", "streamer",
        "former", "kickboxer", "and", "the", "or", "also", "known", "professional"
    }
    return [x for x in norm(value).split() if len(x) >= 3 and x not in stop]


def identity_terms(person: dict) -> tuple[list[str], list[str]]:
    name_tokens = meaningful_tokens(person["name"])
    dis_tokens = meaningful_tokens(person.get("disambiguation", ""))
    primary = []
    for x in name_tokens:
        if x not in primary:
            primary.append(x)
    secondary = []
    for x in reversed(dis_tokens):
        if x not in primary and x not in secondary:
            secondary.append(x)
    return primary, secondary[:5]


def identity_score(candidate: dict, person: dict) -> int:
    blob = norm(" ".join([
        candidate.get("title", ""),
        candidate.get("page", ""),
        candidate.get("image", ""),
        candidate.get("source", ""),
        candidate.get("description", ""),
    ]))
    primary, secondary = identity_terms(person)
    name_norm = norm(person["name"])
    score = 0
    if name_norm and name_norm in blob:
        score += 8
    hits = sum(t in blob for t in primary)
    score += hits * 2
    score += min(3, sum(t in blob for t in secondary))
    if any(x in (candidate.get("page", "") + candidate.get("image", "")).lower() for x in GOOD_DOMAINS):
        score += 2
    if any(x in blob for x in BAD_TEXT):
        score -= 8
    if len(primary) <= 1 or len(name_norm) <= 5:
        if hits == 0:
            return -99
        if not any(t in blob for t in secondary):
            score -= 4
    return score


def is_bad_url(url: str) -> bool:
    host = urlparse(url or "").netloc.lower()
    return any(x in host for x in BAD_DOMAINS)


def commons_search(query: str) -> list[dict]:
    params = {
        "action": "query", "generator": "search", "gsrsearch": query,
        "gsrnamespace": 6, "gsrlimit": 50, "prop": "imageinfo",
        "iiprop": "url|size|mime|extmetadata", "format": "json", "origin": "*",
    }
    try:
        r = requests.get("https://commons.wikimedia.org/w/api.php", params=params,
                         headers={"User-Agent": UA}, timeout=25)
        r.raise_for_status()
        pages = (r.json().get("query") or {}).get("pages") or {}
        out = []
        for page in pages.values():
            infos = page.get("imageinfo") or []
            if not infos:
                continue
            ii = infos[0]
            meta = ii.get("extmetadata") or {}
            desc = " ".join(str((meta.get(k) or {}).get("value", ""))
                            for k in ("ImageDescription", "ObjectName", "Categories"))
            image = ii.get("url")
            if not image:
                continue
            out.append({
                "image": image,
                "page": ii.get("descriptionurl") or f"https://commons.wikimedia.org/wiki/{quote_plus(page.get('title',''))}",
                "title": page.get("title", ""), "source": "Wikimedia Commons",
                "description": BeautifulSoup(desc, "html.parser").get_text(" "),
                "provider": "commons", "query": query,
            })
        return out
    except Exception as e:
        print("COMMONS", query, repr(e), flush=True)
        return []


def ddg_search(query: str) -> list[dict]:
    if DDGS is None:
        return []
    try:
        with DDGS(timeout=18) as d:
            rows = d.images(query, region="wt-wt", safesearch="moderate", max_results=45) or []
        return [{
            "image": x.get("image", ""), "page": x.get("url") or x.get("source", ""),
            "title": x.get("title", ""), "source": x.get("source", ""),
            "description": "", "provider": "ddgs", "query": query,
        } for x in rows if str(x.get("image", "")).startswith("http")]
    except Exception as e:
        print("DDG", query, repr(e), flush=True)
        return []


def bing_search(query: str) -> list[dict]:
    try:
        url = "https://www.bing.com/images/search?q=" + quote_plus(query) + "&form=HDRSC2&first=1"
        r = requests.get(url, headers={"User-Agent": UA}, timeout=25)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        out = []
        for node in soup.select("a.iusc"):
            try:
                meta = json.loads(html.unescape(node.get("m", "{}")))
            except Exception:
                continue
            image = meta.get("murl", "")
            if not image.startswith("http"):
                continue
            out.append({
                "image": image, "page": meta.get("purl", ""), "title": meta.get("t", ""),
                "source": meta.get("s", ""), "description": "", "provider": "bing", "query": query,
            })
            if len(out) >= 45:
                break
        return out
    except Exception as e:
        print("BING", query, repr(e), flush=True)
        return []


def search(query: str, person: dict) -> list[dict]:
    rows = commons_search(query) + ddg_search(query)
    if len(rows) < 35:
        rows += bing_search(query)
    seen = set()
    scored = []
    for c in rows:
        image = c.get("image", "")
        key = image.split("?")[0]
        if not image or key in seen or is_bad_url(image) or is_bad_url(c.get("page", "")):
            continue
        seen.add(key)
        s = identity_score(c, person)
        if s < 3:
            continue
        scored.append((s, 0 if c.get("provider") == "commons" else 1, c))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [x[2] for x in scored]


def detect_face(im: Image.Image) -> bool:
    gray = im.resize((128, 128)).convert("L")
    values = list(gray.getdata())
    if not values:
        return False
    mean = sum(values) / len(values)
    variance = sum((x - mean) ** 2 for x in values) / len(values)
    return variance >= 250


def fetch_image(candidate: dict, slot: str, person: dict):
    try:
        headers = {"User-Agent": UA, "Accept": "image/avif,image/webp,image/*,*/*;q=.8"}
        if candidate.get("page", "").startswith("http"):
            headers["Referer"] = candidate["page"]
        r = requests.get(candidate["image"], headers=headers, timeout=30, allow_redirects=True)
        if r.status_code != 200 or len(r.content) < 25000:
            return None
        content_type = r.headers.get("content-type", "").lower()
        if "image" not in content_type and not re.search(r"\.(jpe?g|png|webp)(\?|$)", r.url, re.I):
            return None
        im = Image.open(io.BytesIO(r.content)); im.seek(0)
        im = ImageOps.exif_transpose(im).convert("RGB")
        w, h = im.size
        if max(w, h) < MIN_LONG or min(w, h) < 320:
            return None
        ratio = w / h
        if ratio > 3.1 or ratio < 0.24:
            return None
        if slot == "full-body" and ratio > 1.9:
            return None
        if max(w, h) > MAX_LONG:
            z = MAX_LONG / max(w, h)
            im = im.resize((round(w * z), round(h * z)), Image.Resampling.LANCZOS)
        face = detect_face(im)
        trusted = candidate.get("provider") == "commons" and identity_score(candidate, person) >= 10
        if not face and not trusted:
            return None
        return im, r.url, face
    except Exception:
        return None


def save_one(folder: Path, person: dict, idx: int, slot: str, candidate: dict,
             im: Image.Image, final_url: str, face: bool) -> dict:
    person_slug = slug(person["name"])
    path = folder / f"{person_slug}_{idx:02d}_{slot}.jpg"
    buf = io.BytesIO(); im.save(buf, "JPEG", quality=94, optimize=True, progressive=True)
    data = buf.getvalue(); path.write_bytes(data)
    return {
        "person_name": person["name"], "disambiguation": person.get("disambiguation", ""),
        "filename": str(path.relative_to(OUT)), "image_type": slot,
        "source_page_url": candidate.get("page", ""), "direct_image_url": final_url,
        "source_domain": urlparse(candidate.get("page") or final_url).netloc,
        "width": im.width, "height": im.height, "file_format": "JPEG",
        "sha256": hashlib.sha256(data).hexdigest(), "perceptual_hash": str(imagehash.phash(im)),
        "identity_confidence": "high",
        "identity_evidence": f"exact-name/context search; metadata_score={identity_score(candidate, person)}; provider={candidate.get('provider')}; title={candidate.get('title','')[:180]}; image_detail_check={face}",
        "search_query": candidate.get("query", ""), "provider": candidate.get("provider", ""), "notes": "",
    }


def harvest_person(person: dict) -> dict:
    name = person["name"]
    folder = OUT / slug(name); folder.mkdir(parents=True, exist_ok=True)
    accepted = []; hashes = []; used_urls = set(); failures = []
    context = person.get("disambiguation", "")
    for idx, (slot, phrases) in enumerate(SLOTS, start=1):
        found = False
        queries = [f'"{name}" {context} {phrase} photo' for phrase in phrases]
        queries += [f'"{name}" {context} press photo high resolution', f'"{name}" {context} public appearance photo']
        attempts = 0
        for query in queries:
            time.sleep(random.uniform(0.1, 0.45))
            for candidate in search(query, person):
                attempts += 1
                if attempts > 80:
                    break
                if candidate["image"] in used_urls:
                    continue
                got = fetch_image(candidate, slot, person)
                if not got:
                    continue
                im, final_url, face = got
                ph = imagehash.phash(im)
                if any(ph - old <= 10 for old in hashes):
                    continue
                row = save_one(folder, person, idx, slot, candidate, im, final_url, face)
                if row["sha256"] in {x["sha256"] for x in accepted}:
                    continue
                accepted.append(row); hashes.append(ph); used_urls.add(candidate["image"])
                found = True; break
            if found or attempts > 80:
                break
        if not found:
            failures.append(f"{slot}: no valid distinct candidate after {attempts} results")
    if len(accepted) != 5:
        shutil.rmtree(folder, ignore_errors=True); accepted = []
    print(f"[{name}] {len(accepted)}/5 " + ("OK" if accepted else "FAILED | " + "; ".join(failures)), flush=True)
    return {"person": person, "rows": accepted, "failures": failures}


def make_contact_sheet(results: list[dict]) -> None:
    thumb_w, thumb_h, left, row_h = 180, 180, 210, 215
    canvas = Image.new("RGB", (left + thumb_w * 5, max(1, len(results)) * row_h), "white")
    draw = ImageDraw.Draw(canvas); font = ImageFont.load_default()
    for row_idx, result in enumerate(results):
        y = row_idx * row_h
        draw.text((8, y + 8), result["person"]["name"], fill="black", font=font)
        draw.text((8, y + 27), f'{len(result["rows"])}/5', fill="black", font=font)
        for col, item in enumerate(result["rows"]):
            try:
                im = Image.open(OUT / item["filename"]).convert("RGB")
                im.thumbnail((thumb_w - 8, thumb_h - 8))
                tile = Image.new("RGB", (thumb_w, thumb_h), "#eeeeee")
                tile.paste(im, ((thumb_w-im.width)//2, (thumb_h-im.height)//2))
                canvas.paste(tile, (left + col * thumb_w, y))
                draw.text((left + col*thumb_w + 4, y + 184), item["image_type"][:20], fill="black", font=font)
            except Exception:
                pass
    canvas.save(OUT / "contact_sheet.jpg", quality=88)


def main() -> None:
    all_people = json.load(open("people_next93.json", encoding="utf-8"))
    start = BATCH_INDEX * BATCH_SIZE
    people = all_people[start:start + BATCH_SIZE]
    if OUT.exists(): shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    results = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(harvest_person, p): p for p in people}
        for future in as_completed(futures):
            try: results.append(future.result())
            except Exception as e:
                p = futures[future]
                print(f'[{p["name"]}] ERROR {e!r}', flush=True)
                results.append({"person": p, "rows": [], "failures": [repr(e)]})
    order = {p["name"]: i for i, p in enumerate(people)}
    results.sort(key=lambda x: order[x["person"]["name"]])
    rows = [r for result in results for r in result["rows"]]
    fields = ["person_name", "disambiguation", "filename", "image_type", "source_page_url", "direct_image_url", "source_domain", "width", "height", "file_format", "sha256", "perceptual_hash", "identity_confidence", "identity_evidence", "search_query", "provider", "notes"]
    with (OUT / "manifest.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields); writer.writeheader(); writer.writerows(rows)
    failures = [{
        "person_name": result["person"]["name"], "disambiguation": result["person"].get("disambiguation", ""),
        "images_accepted": len(result["rows"]), "images_required": 5, "reason": " | ".join(result["failures"]),
    } for result in results if len(result["rows"]) != 5]
    with (OUT / "failures.csv").open("w", encoding="utf-8-sig", newline="") as f:
        fields2 = ["person_name", "disambiguation", "images_accepted", "images_required", "reason"]
        writer = csv.DictWriter(f, fieldnames=fields2); writer.writeheader(); writer.writerows(failures)
    complete = sum(len(x["rows"]) == 5 for x in results)
    summary = f"Batch index: {BATCH_INDEX}\nPeople requested: {len(people)}\nPeople complete with exactly 5 images: {complete}\nPeople failed/incomplete: {len(people)-complete}\nAccepted images: {len(rows)}\n"
    (OUT / "README.txt").write_text(summary, encoding="utf-8")
    make_contact_sheet(results)
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        for p in OUT.rglob("*"):
            if p.is_file(): z.write(p, p.relative_to(OUT.parent))
    print(summary, flush=True)


if __name__ == "__main__":
    main()
