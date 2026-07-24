from __future__ import annotations

import html
import json
import math
import re
import shutil
import time
from pathlib import Path
from urllib.parse import quote_plus, urlparse

from bs4 import BeautifulSoup

import tools.harvest_sony18_fast as f

h = f.h

# Fast, strict, quality-first completion pass.
h.GROUP_SIZE = 1
h.STYLE_TARGET = 24
h.CHAR_REFS = 4
h.TIMEOUT = 12

# Additional reputable/static image hosts. Search metadata still has to match
# the film and character exactly before any file is accepted.
f.SAFE_HOST_SUFFIXES = tuple(dict.fromkeys(f.SAFE_HOST_SUFFIXES + (
    "blogger.googleusercontent.com",
    "static.wikia.nocookie.net",
    "images.fanpop.com",
    "images2.fanpop.com",
    "cdn.vox-cdn.com",
    "static1.colliderimages.com",
    "static0.gamerantimages.com",
    "deadline.com",
    "ew.com",
    "people.com",
    "playbill.com",
    "digitalspy.com",
    "suntimes.com",
    "toledoblade.com",
    "wp.com",
    "wordpress.com",
    "cloudfront.net",
    "akamaized.net",
    "ssl-images-amazon.com",
    "mm.bing.net",
    "bing.net",
)))

STYLE_REJECT = {
    "poster", "posters", "logo", "logos", "wallpaper", "wallpapers",
    "merch", "merchandise", "toy", "toys", "figurine", "dvd", "blu ray",
    "soundtrack", "spotify", "book cover", "amazon.com", "vacation spots",
    "flatness", "straightness", "hairstyle", "hair clips", "best in travel",
    "review score", "box office chart",
}
CHAR_REJECT = {
    "toy", "toys", "costume", "cosplay", "funko", "lego", "coloring page",
    "coloring pages", "birthday cake", "t shirt", "shirt", "merchandise",
    "flatness", "straightness", "vacation spots", "hairstyle", "hair clips",
    "best in travel", "spotify", "logo", "book cover",
}
WORD_STOP = {
    "the", "and", "with", "from", "into", "versus", "movie", "film",
    "animated", "animation", "official", "pictures", "christmas",
}
MARKERS = {"movie", "film", "animated", "animation", "netflix", "sony", "character", "still", "scene"}


def norm(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", html.unescape(str(value or "")).lower()))


def meaningful(value: str, *, keep_christmas: bool = True) -> list[str]:
    stop = WORD_STOP - ({"christmas"} if keep_christmas else set())
    return [x for x in norm(value).split() if len(x) > 2 and x not in stop]


def token_hits(tokens: list[str], hay: str) -> int:
    return sum(1 for token in tokens if re.search(rf"\b{re.escape(token)}\b", hay))


def film_relevance(film: dict, metadata: str) -> tuple[bool, int]:
    hay = norm(metadata)
    tokens = meaningful(film["title"])
    hits = token_hits(tokens, hay)
    if len(tokens) >= 3:
        required = max(2, math.ceil(len(tokens) * 0.6))
    elif len(tokens) == 2:
        required = 2
    else:
        required = 1
    marker = str(film.get("year")) in hay or any(re.search(rf"\b{m}\b", hay) for m in MARKERS)
    # One-word titles such as Vivo are dangerously ambiguous; require a film marker.
    ok = hits >= required and (len(tokens) > 1 or marker)
    return ok, hits + int(marker)


def character_relevance(film: dict, character: str, metadata: str) -> tuple[bool, int]:
    hay = norm(metadata)
    film_ok, film_score = film_relevance(film, hay)
    char_tokens = meaningful(character, keep_christmas=False)
    if not char_tokens:
        char_tokens = [x for x in norm(character).split() if len(x) > 1]
    char_hits = token_hits(char_tokens, hay)
    char_required = 1 if len(char_tokens) <= 2 else 2
    return film_ok and char_hits >= char_required, film_score + char_hits * 3


def rejected(metadata: str, terms: set[str]) -> bool:
    hay = norm(metadata)
    return any(norm(term) in hay for term in terms)


def strict_tmdb_candidates(film: dict, log: list[str]) -> list[dict]:
    rows = f.tmdb_candidates(film, log)
    accepted = []
    for row in rows:
        host = urlparse(row.get("url", "")).netloc.lower()
        if not (host.endswith("image.tmdb.org") or host.endswith("media.themoviedb.org")):
            continue
        alt = row.get("alt", "")
        ok, score = film_relevance(film, f"{alt} {film['title']} {film['year']} movie")
        # The page itself has already been resolved to the exact movie. Require either
        # its title in alt text or a TMDB original/backdrop URL shape.
        url = row.get("url", "")
        title_hits = token_hits(meaningful(film["title"]), norm(alt))
        if not ok or (title_hits == 0 and "/t/p/" not in url):
            continue
        row = dict(row)
        row["relevance_score"] = score
        accepted.append(row)
    log.append(f"strict_tmdb_candidates={len(accepted)}")
    return accepted


def strict_bing_candidates(
    film: dict,
    query: str,
    context: str,
    log: list[str],
    *,
    character: str | None = None,
    style: bool = False,
    limit: int = 30,
) -> list[dict]:
    url = "https://www.bing.com/images/search?q=" + quote_plus(query) + "&form=HDRSC2&first=1"
    try:
        text = f.fast_request(url, headers={"Accept-Language": "en-US,en;q=0.9"})
    except Exception as exc:
        log.append(f"strict_bing_failed={type(exc).__name__} query={query}")
        return []
    soup = BeautifulSoup(text, "html.parser")
    rows, seen = [], set()
    for tag in soup.select("a.iusc"):
        raw = html.unescape(tag.get("m", ""))
        try:
            item = json.loads(raw)
        except Exception:
            continue
        original = item.get("murl")
        thumbnail = item.get("turl")
        source_page = item.get("purl") or ""
        title = item.get("t") or item.get("desc") or ""
        metadata = f"{title} {source_page}"
        if character:
            ok, score = character_relevance(film, character, metadata)
            if rejected(metadata, CHAR_REJECT):
                ok = False
        else:
            ok, score = film_relevance(film, metadata)
            if style and rejected(metadata, STYLE_REJECT):
                ok = False
        if not ok:
            continue
        image_url = original if original and f.safe_host(original) else thumbnail
        if not image_url or not f.safe_host(image_url) or image_url in seen:
            continue
        if not f.safe_text(image_url, source_page, title, query):
            continue
        seen.add(image_url)
        row = {
            "url": image_url,
            "alt": title,
            "context": context,
            "source_type": "character_reference" if character else "trusted_style_search",
            "source_page": source_page,
            "query": query,
            "relevance_score": score,
        }
        if character:
            row["character_tag"] = character
        rows.append(row)
        if len(rows) >= limit:
            break
    rows.sort(key=lambda r: r.get("relevance_score", 0), reverse=True)
    log.append(f"strict_bing_candidates={len(rows)} character={character or '-'} query={query}")
    return rows


def quality_download_candidates(candidates, film_dir: Path, log: list[str], prefix: str, cap: int) -> list[dict]:
    output, seen_urls = [], set()
    folder = film_dir / "_TRUSTED_POOL"
    folder.mkdir(parents=True, exist_ok=True)
    for item in candidates:
        url = item.get("url")
        if not url or url in seen_urls or not f.safe_host(url):
            continue
        seen_urls.add(url)
        try:
            data = f.fast_request(
                url,
                binary=True,
                headers={"Referer": item.get("source_page") or f._CURRENT_PAGE_URL or "https://www.imdb.com/"},
            )
            if len(data) < 16000:
                continue
            destination = folder / f"{prefix}_{len(output):03d}.jpg"
            metadata = h.normalize_image(data, destination)
            if not metadata:
                continue
            if metadata["brightness"] < 8 or metadata["brightness"] > 247 or metadata["quality"] < 3.5:
                destination.unlink(missing_ok=True)
                continue
            # Style refs must be landscape. Character refs may be portrait or square.
            if item.get("source_type") in {"trusted_tmdb_backdrop", "trusted_style_search"} and not metadata.get("landscape"):
                destination.unlink(missing_ok=True)
                continue
            metadata.update({
                "url": url,
                "alt": item.get("alt", ""),
                "context": item.get("context", ""),
                "source_type": item.get("source_type", "trusted_gallery"),
                "source_page": item.get("source_page"),
                "query": item.get("query"),
                "relevance_score": item.get("relevance_score", 0),
                "character_tag": item.get("character_tag"),
            })
            output.append(metadata)
            if len(output) >= cap:
                break
        except Exception:
            continue
    log.append(f"quality_downloaded={len(output)} prefix={prefix}")
    return output


def quality_trusted_gallery_pool(film: dict, film_dir: Path, log: list[str]) -> list[dict]:
    # Exact movie backdrops only: no generic search cards from the TMDB page.
    base_candidates = f.imdb_candidates(film, log) + strict_tmdb_candidates(film, log)
    base = quality_download_candidates(base_candidates, film_dir, log, "gallery", 90)

    # Strongly matched style search is only a fallback for sparse/new releases.
    if len(base) < 30:
        style_rows = []
        for query in [
            f'"{film["title"]}" {film["year"]} animated movie still',
            f'"{film["title"]}" official movie scene Sony Animation',
        ]:
            style_rows.extend(strict_bing_candidates(
                film, query,
                f"Strict exact-title style search for {film['title']} ({film['year']})",
                log, style=True, limit=30,
            ))
        base.extend(quality_download_candidates(style_rows, film_dir, log, "style", 35))

    character_pool: list[dict] = []
    for index, character in enumerate(film.get("characters", [])):
        rows = []
        for query in [
            f'"{film["title"]}" "{character}" character image',
            f'"{character}" "{film["title"]}" animated movie still',
            f'{film["title"]} {character} official character',
        ]:
            rows.extend(strict_bing_candidates(
                film, query,
                f"Verified character reference search for {character} in {film['title']}",
                log, character=character, limit=18,
            ))
        # URL-level dedupe before download.
        unique = list({row["url"]: row for row in rows}.values())
        unique.sort(key=lambda r: r.get("relevance_score", 0), reverse=True)
        character_pool.extend(quality_download_candidates(unique, film_dir, log, f"char_{index:02d}", 8))
        time.sleep(0.1)

    # Lower threshold so separate shots are retained while exact duplicates disappear.
    combined = h.dedupe(base + character_pool, threshold=4, cap=260)
    log.append(f"quality_unique_pool={len(combined)}")
    return combined


def quality_video_pool(film, page_url, text, film_dir, log, needed=True):
    # Embedded official trailers yield useful clean thumbnails. Avoid slow/failing
    # yt-dlp downloads; the exact TMDB backdrop gallery supplies the remaining shots.
    urls = h.extract_video_urls(page_url, text)
    frames = f.youtube_thumbnail_frames(urls, film_dir, log)
    return h.dedupe(frames, threshold=5, cap=6)


def quality_select_style(pool: list[dict]) -> list[dict]:
    allowed = {
        "official_site", "official_video", "trusted_tmdb_backdrop",
        "trusted_imdb_still", "trusted_style_search",
    }
    landscape = [x for x in pool if x.get("landscape") and x.get("source_type") in allowed]
    official = [x for x in landscape if x.get("source_type") == "official_site"]
    tmdb = [x for x in landscape if x.get("source_type") == "trusted_tmdb_backdrop"]
    video = [x for x in landscape if x.get("source_type") == "official_video"]
    imdb = [x for x in landscape if x.get("source_type") == "trusted_imdb_still"]
    search = [x for x in landscape if x.get("source_type") == "trusted_style_search"]
    selected: list[dict] = []
    for bucket, cap in [(official, 14), (tmdb, 24), (video, 4), (imdb, 12), (search, 12)]:
        remaining = [x for x in bucket if x not in selected]
        if remaining and len(selected) < h.STYLE_TARGET:
            selected += h.diverse(remaining, min(cap, h.STYLE_TARGET - len(selected), len(remaining)))
    return selected[:h.STYLE_TARGET]


def quality_pick_character(pool: list[dict], name: str):
    exact = [x for x in pool if x.get("source_type") == "character_reference" and x.get("character_tag") == name]
    exact.sort(key=lambda x: (x.get("relevance_score", 0), x.get("quality", 0)), reverse=True)
    exact = h.dedupe(exact, threshold=4, cap=h.CHAR_REFS)
    if exact:
        return exact[:h.CHAR_REFS], "strict_film_and_character_metadata_match"
    return [], "no_verified_reference_found"


# Replace the permissive parts of the fast pass.
f.trusted_gallery_pool = quality_trusted_gallery_pool
h.video_pool = quality_video_pool
h.select_style = quality_select_style
h.pick_character = quality_pick_character

# Add QA truth after each film is written.
_previous_process = h.process


def quality_process(film, root):
    result = _previous_process(film, root)
    film_dir = Path(root) / f"{film['year']}_{h.slugify(film['title'])}"
    manifest_path = film_dir / "MANIFEST.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        verified_chars = sum(
            1 for data in manifest.get("characters", {}).values()
            if data.get("method") == "strict_film_and_character_metadata_match" and data.get("refs")
        )
        manifest["qa"] = {
            "source_policy": "strict safe-source allowlist plus exact film/character metadata match",
            "perceptual_deduplication": True,
            "style_excludes_unverified_exact_search_results": True,
            "character_generic_fallback_disabled": True,
            "verified_character_count": verified_chars,
            "requested_character_count": len(manifest.get("characters", {})),
            "manual_contact_sheet_review_required": True,
        }
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


h.process = quality_process

if __name__ == "__main__":
    h.main()
