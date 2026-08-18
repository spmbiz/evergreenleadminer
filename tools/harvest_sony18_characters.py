from __future__ import annotations

import argparse
import html
import json
import math
import re
import shutil
import time
from pathlib import Path
from urllib.parse import quote_plus

from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont
import imagehash

import tools.harvest_sony18_fast as f

h = f.h
h.GROUP_SIZE = 1
h.TIMEOUT = 12

f.SAFE_HOST_SUFFIXES = tuple(dict.fromkeys(f.SAFE_HOST_SUFFIXES + (
    "bing.com", "bing.net", "blogger.googleusercontent.com",
    "static.wikia.nocookie.net", "images.fanpop.com", "images2.fanpop.com",
    "cdn.vox-cdn.com", "static1.colliderimages.com", "static0.gamerantimages.com",
    "deadline.com", "ew.com", "people.com", "playbill.com", "digitalspy.com",
    "suntimes.com", "toledoblade.com", "wp.com", "wordpress.com",
    "cloudfront.net", "akamaized.net", "ssl-images-amazon.com",
    "alphacoders.com", "avatarko.ru", "naiz.eus",
)))

BLOCK = {
    "porn", "xxx", "hentai", "rule34", "nsfw", "nude", "nudity",
    "onlyfans", "sex", "erotic", "fetish", "booru", "deviantart",
    "pinterest", "reddit", "tumblr", "fanart", "cosplay", "costume",
    "toy", "toys", "funko", "lego", "coloring", "birthday cake",
    "t shirt", "merchandise", "flatness", "straightness", "vacation spots",
    "hairstyle", "hair clips", "spotify", "logo", "book cover",
}
STOP = {"the", "and", "with", "from", "into", "versus", "movie", "film", "animated", "animation", "official", "pictures", "chance", "summer", "vacation"}
GENERIC_NAMES = {"steve", "rosa", "manny", "barb", "dennis", "gwen", "pedro", "vivo", "marta", "jonathan", "mavis", "sam", "earl", "arthur", "santa"}


def norm(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", html.unescape(str(value or "")).lower()))


def tokens(value: str, stop=STOP) -> list[str]:
    return [x for x in norm(value).split() if len(x) > 2 and x not in stop]


def hits(words: list[str], hay: str) -> int:
    return sum(bool(re.search(rf"\b{re.escape(w)}\b", hay)) for w in words)


def reject(metadata: str) -> bool:
    hay = norm(metadata)
    return any(norm(term) in hay for term in BLOCK)


def relevance(film: dict, character: str, metadata: str) -> tuple[bool, int]:
    hay = norm(metadata)
    c_tokens = tokens(character, stop=set())
    f_tokens = tokens(film["title"])
    c_hits = hits(c_tokens, hay)
    f_hits = hits(f_tokens, hay)
    all_char = c_hits >= max(1, min(2, len(c_tokens)))
    generic = norm(character) in GENERIC_NAMES or len(c_tokens) <= 1
    page_marker = any(x in hay for x in ["character", "wiki", "fandom", "animated", "animation", "movie", "film", "sony", "netflix"])
    if generic:
        required_film = min(2, max(1, len(f_tokens)))
        ok = all_char and f_hits >= required_film
    else:
        # A distinctive full two-word name is strong enough when the result is clearly
        # an animation/character page; otherwise require one film-title token too.
        ok = all_char and (f_hits >= 1 or page_marker)
    return ok and not reject(metadata), c_hits * 5 + f_hits * 3 + int(page_marker)


def query_bing(film: dict, character: str, query: str, log: list[str], limit=40) -> list[dict]:
    url = "https://www.bing.com/images/search?q=" + quote_plus(query) + "&form=HDRSC2&first=1"
    try:
        text = f.fast_request(url, headers={"Accept-Language": "en-US,en;q=0.9"})
    except Exception as exc:
        log.append(f"bing_error={type(exc).__name__} query={query}")
        return []
    soup = BeautifulSoup(text, "html.parser")
    rows, seen = [], set()
    for tag in soup.select("a.iusc"):
        try:
            item = json.loads(html.unescape(tag.get("m", "")))
        except Exception:
            continue
        title = item.get("t") or item.get("desc") or ""
        source_page = item.get("purl") or ""
        metadata = f"{title} {source_page}"
        ok, score = relevance(film, character, metadata)
        if not ok:
            continue
        original = item.get("murl")
        thumbnail = item.get("turl")
        image_url = original if original and f.safe_host(original) else thumbnail
        if not image_url or not f.safe_host(image_url) or image_url in seen:
            continue
        if not f.safe_text(image_url, title, source_page, query):
            continue
        seen.add(image_url)
        rows.append({
            "url": image_url,
            "source_page": source_page,
            "title": title,
            "query": query,
            "score": score,
            "used_thumbnail": image_url == thumbnail,
        })
        if len(rows) >= limit:
            break
    rows.sort(key=lambda row: row["score"], reverse=True)
    log.append(f"query_candidates={len(rows)} character={character} query={query}")
    return rows


def download(rows: list[dict], folder: Path, log: list[str], cap=10) -> list[dict]:
    folder.mkdir(parents=True, exist_ok=True)
    out, hashes = [], []
    for row in rows:
        try:
            data = f.fast_request(row["url"], binary=True, headers={"Referer": row.get("source_page") or "https://www.bing.com/"})
            if len(data) < 8000:
                continue
            temp = folder / f"candidate_{len(out):02d}.jpg"
            meta = h.normalize_image(data, temp)
            if not meta or meta["quality"] < 2.8 or meta["brightness"] < 6 or meta["brightness"] > 249:
                temp.unlink(missing_ok=True)
                continue
            ph = imagehash.hex_to_hash(meta["phash"])
            if any(ph - old < 4 for old in hashes):
                temp.unlink(missing_ok=True)
                continue
            hashes.append(ph)
            meta.update(row)
            out.append(meta)
            if len(out) >= cap:
                break
        except Exception:
            continue
    log.append(f"downloaded={len(out)} folder={folder.name}")
    return out


def contact_sheet(items: list[tuple[Path, str]], dest: Path, cols=4, thumb=(320, 320)):
    if not items:
        return
    w, h = thumb
    margin, label_h = 12, 34
    rows = math.ceil(len(items) / cols)
    canvas = Image.new("RGB", (margin + cols*(w+margin), margin + rows*(h+label_h+margin)), (24,24,24))
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 13)
    except Exception:
        font = ImageFont.load_default()
    for i, (path, label) in enumerate(items):
        im = Image.open(path).convert("RGB")
        im.thumbnail((w,h))
        tile = Image.new("RGB", (w,h), (5,5,5))
        tile.paste(im, ((w-im.width)//2, (h-im.height)//2))
        x = margin + (i % cols)*(w+margin)
        y = margin + (i // cols)*(h+label_h+margin)
        canvas.paste(tile, (x,y))
        draw.text((x,y+h+5), label[:46], font=font, fill="white")
    canvas.save(dest, "JPEG", quality=90, optimize=True)


def process(film: dict, root: Path) -> dict:
    film_dir = root / f"{film['year']}_{h.slugify(film['title'])}"
    if film_dir.exists():
        shutil.rmtree(film_dir)
    film_dir.mkdir(parents=True)
    log: list[str] = []
    manifest_chars = {}
    sheet_items = []
    for character in film.get("characters", []):
        rows = []
        queries = [
            f'"{film["title"]}" "{character}" character',
            f'"{character}" "{film["title"]}" movie image',
            f'{film["title"]} {character} animated character',
            f'{character} {film["title"]} Sony Pictures Animation',
        ]
        for query in queries:
            rows.extend(query_bing(film, character, query, log, limit=35))
        unique = list({row["url"]: row for row in rows}.values())
        unique.sort(key=lambda row: row["score"], reverse=True)
        temp_dir = film_dir / "_CANDIDATES" / re.sub(r"[^A-Za-z0-9_-]+", "_", character).strip("_")
        refs = download(unique, temp_dir, log, cap=10)
        final_dir = film_dir / "CHARACTER_PACK" / re.sub(r"[^A-Za-z0-9_-]+", "_", character).strip("_")
        final_dir.mkdir(parents=True, exist_ok=True)
        selected = refs[:4]
        records=[]
        for idx, item in enumerate(selected,1):
            dest=final_dir/f"ref_{idx:02d}.jpg"
            shutil.copy2(item["path"],dest)
            records.append({
                "file":str(dest.relative_to(film_dir)),
                "source_url":item["url"],
                "source_page":item.get("source_page"),
                "title":item.get("title"),
                "query":item.get("query"),
                "used_bing_thumbnail":item.get("used_thumbnail"),
                "width":item.get("width"),"height":item.get("height"),
                "sha256":item.get("sha256"),"relevance_score":item.get("score"),
            })
            sheet_items.append((dest,f"{character} {idx}"))
        manifest_chars[character]={
            "verified_metadata_match":bool(records),
            "refs":records,
        }
        time.sleep(0.1)
    shutil.rmtree(film_dir/"_CANDIDATES",ignore_errors=True)
    contact_sheet(sheet_items,film_dir/"CHARACTER_CONTACT_SHEET.jpg")
    covered=sum(bool(v["refs"]) for v in manifest_chars.values())
    manifest={
        "title":film["title"],"year":film["year"],"studio":"Sony Pictures Animation",
        "requested_characters":len(manifest_chars),"covered_characters":covered,
        "character_refs":sum(len(v["refs"]) for v in manifest_chars.values()),
        "characters":manifest_chars,
        "qa":{
            "generic_fallback_disabled":True,
            "exact_character_metadata_required":True,
            "unsafe_terms_blocked":True,
            "perceptual_deduplication":True,
            "manual_contact_sheet_review_required":True,
        },
        "log":log,
    }
    (film_dir/"CHARACTER_MANIFEST.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
    (film_dir/"LOG.txt").write_text("\n".join(log),encoding="utf-8")
    return {"title":film["title"],"covered":covered,"requested":len(manifest_chars),"refs":manifest["character_refs"]}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--group",type=int,required=True)
    parser.add_argument("--output",default="output")
    args=parser.parse_args()
    film=h.FILMS[args.group]
    root=Path(args.output)/f"sony_characters_{args.group+1:02d}"
    root.mkdir(parents=True,exist_ok=True)
    result=process(film,root)
    (root/"SUMMARY.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(result,ensure_ascii=False))

if __name__=="__main__":
    main()
