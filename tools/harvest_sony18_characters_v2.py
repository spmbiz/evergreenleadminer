from __future__ import annotations

import html
import json
import re
import time
from urllib.parse import quote_plus, urlparse

import tools.harvest_sony18_characters as c

f = c.f

f.SAFE_HOST_SUFFIXES = tuple(dict.fromkeys(f.SAFE_HOST_SUFFIXES + (
    "duckduckgo.com",
    "external-content.duckduckgo.com",
    "images.duckduckgo.com",
    "wikimedia.org",
    "wikipedia.org",
    "fandom.com",
    "nocookie.net",
)))

# Keep sequel-defining words in the match. The first pass treated "Into" as a
# stopword and could accidentally accept Across/Beyond material.
c.STOP.discard("into")
c.BLOCK.update({
    "meme", "memes", "tutorial", "how to draw", "drawing", "fan art",
    "fanart", "reaction", "live action", "behind the scenes", "cosplay",
    "costume", "analysis video", "character design analysis",
})

TRUSTED_SOURCE_HINTS = (
    "sonypictures", "netflix", "imdb.com", "fandom.com", "wikia.com",
    "wikipedia.org", "wikimedia.org", "polygon.com", "variety.com",
    "deadline.com", "collider.com", "screenrant.com", "youtube.com",
)


def _vqd(query: str) -> str | None:
    url = "https://duckduckgo.com/?q=" + quote_plus(query)
    text = f.fast_request(
        url,
        headers={
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://duckduckgo.com/",
        },
    )
    for pattern in [
        r"vqd=['\"]([\d-]+)['\"]",
        r"vqd=([\d-]+)&",
        r'"vqd"\s*:\s*"([\d-]+)"',
    ]:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return None


def sequel_conflict(film: dict, metadata: str) -> bool:
    title = c.norm(film["title"])
    hay = c.norm(metadata)
    if "into the spider verse" in title and any(x in hay for x in ["across the spider verse", "beyond the spider verse"]):
        return True
    if "across the spider verse" in title and any(x in hay for x in ["into the spider verse", "beyond the spider verse"]):
        return True
    if title == "hotel transylvania" and any(x in hay for x in ["hotel transylvania 2", "hotel transylvania 3", "transformania"]):
        return True
    if "hotel transylvania 2" in title and any(x in hay for x in ["hotel transylvania 3", "transformania"]):
        return True
    if "hotel transylvania 3" in title and "transformania" in hay:
        return True
    if title == "cloudy with a chance of meatballs" and "meatballs 2" in hay:
        return True
    return False


def source_bonus(source_page: str, title: str) -> int:
    low = f"{source_page} {title}".lower()
    bonus = 5 if any(hint in low for hint in TRUSTED_SOURCE_HINTS) else 0
    if "youtube.com" in low:
        # YouTube thumbnails are useful only for actual movie clips/scenes.
        if any(word in low for word in ["scene", "movie clip", "official clip", "trailer"]):
            bonus += 2
        else:
            return -100
    if any(word in low for word in ["rare-gallery", "wallpaper", "alphacoders"]):
        bonus -= 1
    return bonus


def query_ddg(film: dict, character: str, query: str, log: list[str], limit=40) -> list[dict]:
    try:
        token = _vqd(query)
    except Exception as exc:
        log.append(f"ddg_token_error={type(exc).__name__} query={query}")
        return []
    if not token:
        log.append(f"ddg_token_missing query={query}")
        return []
    endpoint = (
        "https://duckduckgo.com/i.js?l=us-en&o=json&p=1&f=,,,&q="
        + quote_plus(query)
        + "&vqd="
        + token
    )
    try:
        raw = f.fast_request(
            endpoint,
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://duckduckgo.com/",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        payload = json.loads(raw)
    except Exception as exc:
        log.append(f"ddg_results_error={type(exc).__name__} query={query}")
        return []

    rows, seen = [], set()
    exact_film = c.norm(film["title"])
    exact_character = c.norm(character)
    for item in payload.get("results", []):
        title = html.unescape(item.get("title") or "")
        source_page = item.get("url") or item.get("source") or ""
        metadata = f"{title} {source_page}"
        ok, score = c.relevance(film, character, metadata)
        if not ok or sequel_conflict(film, metadata):
            continue
        bonus = source_bonus(source_page, title)
        if bonus <= -100:
            continue
        normalized_metadata = c.norm(metadata)
        if exact_film in normalized_metadata:
            score += 7
        if exact_character in normalized_metadata:
            score += 5
        score += bonus
        original = item.get("image")
        thumbnail = item.get("thumbnail")
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
            "search_engine": "duckduckgo_images",
        })
        if len(rows) >= limit:
            break
    rows.sort(key=lambda row: row["score"], reverse=True)
    log.append(f"ddg_candidates={len(rows)} character={character} query={query}")
    time.sleep(0.35)
    return rows


c.query_bing = query_ddg

if __name__ == "__main__":
    c.main()
