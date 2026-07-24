from __future__ import annotations

import html
import json
import re
import time
from urllib.parse import quote_plus

import tools.harvest_sony18_quality as q

f = q.f
h = q.h

f.SAFE_HOST_SUFFIXES = tuple(dict.fromkeys(f.SAFE_HOST_SUFFIXES + (
    "duckduckgo.com",
    "external-content.duckduckgo.com",
    "images.duckduckgo.com",
    "wikimedia.org",
    "wikipedia.org",
    "fandom.com",
    "nocookie.net",
)))


def _vqd(query: str) -> str | None:
    text = f.fast_request(
        "https://duckduckgo.com/?q=" + quote_plus(query),
        headers={"Accept-Language": "en-US,en;q=0.9", "Referer": "https://duckduckgo.com/"},
    )
    for pattern in [r"vqd=['\"]([\d-]+)['\"]", r"vqd=([\d-]+)&", r'"vqd"\s*:\s*"([\d-]+)"']:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return None


def ddg_candidates(
    film: dict,
    query: str,
    context: str,
    log: list[str],
    *,
    character: str | None = None,
    style: bool = False,
    limit: int = 35,
) -> list[dict]:
    try:
        token = _vqd(query)
        if not token:
            log.append(f"ddg_token_missing query={query}")
            return []
        endpoint = (
            "https://duckduckgo.com/i.js?l=us-en&o=json&p=1&f=,,,&q="
            + quote_plus(query)
            + "&vqd=" + token
        )
        payload = json.loads(f.fast_request(
            endpoint,
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://duckduckgo.com/",
                "X-Requested-With": "XMLHttpRequest",
            },
        ))
    except Exception as exc:
        log.append(f"ddg_results_error={type(exc).__name__} query={query}")
        return []

    rows, seen = [], set()
    for item in payload.get("results", []):
        title = html.unescape(item.get("title") or "")
        source_page = item.get("url") or item.get("source") or ""
        metadata = f"{title} {source_page}"
        if character:
            ok, score = q.character_relevance(film, character, metadata)
            if q.rejected(metadata, q.CHAR_REJECT):
                ok = False
        else:
            ok, score = q.film_relevance(film, metadata)
            if style and q.rejected(metadata, q.STYLE_REJECT):
                ok = False
        if not ok:
            continue
        original = item.get("image")
        thumbnail = item.get("thumbnail")
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
            "search_engine": "duckduckgo_images",
        }
        if character:
            row["character_tag"] = character
        rows.append(row)
        if len(rows) >= limit:
            break
    rows.sort(key=lambda row: row.get("relevance_score", 0), reverse=True)
    log.append(f"ddg_candidates={len(rows)} character={character or '-'} query={query}")
    time.sleep(0.35)
    return rows


q.strict_bing_candidates = ddg_candidates

if __name__ == "__main__":
    h.main()
