import html
import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import quote_plus, urljoin, urlparse

from bs4 import BeautifulSoup

import tools.harvest_sony18 as h

# One film per job. The completion run only schedules the 14 incomplete titles.
h.GROUP_SIZE = 1
h.TIMEOUT = 15
h.STYLE_TARGET = 24
h.CHAR_REFS = 4

_CURRENT_FILM = None
_CURRENT_PAGE_URL = None
_ORIGINAL_PROCESS = h.process
_ORIGINAL_SITE_POOL = h.download_site_pool
_ORIGINAL_EXTRACT_VIDEO_URLS = h.extract_video_urls

SAFE_HOST_SUFFIXES = (
    "sonypicturesanimation.com",
    "sonypictures.com",
    "images.squarespace-cdn.com",
    "imdb.com",
    "media-amazon.com",
    "themoviedb.org",
    "image.tmdb.org",
    "media.themoviedb.org",
    "netflix.com",
    "nflxso.net",
    "ctfassets.net",
    "ytimg.com",
)

BLOCKED_TERMS = {
    "porn", "xxx", "hentai", "rule34", "nsfw", "nude", "nudity",
    "onlyfans", "sex", "erotic", "fetish", "booru", "deviantart",
    "pinterest", "reddit", "tumblr", "fanart", "wallpaperflare",
}


def fast_request(url, binary=False, headers=None):
    last = None
    merged = dict(h.SESSION.headers)
    if headers:
        merged.update(headers)
    for attempt in range(3):
        try:
            response = h.SESSION.get(
                url,
                timeout=h.TIMEOUT,
                allow_redirects=True,
                headers=merged,
            )
            response.raise_for_status()
            return response.content if binary else response.text
        except Exception as exc:
            last = exc
            time.sleep(0.8 + attempt * 0.7)
    raise last


h.request = fast_request


def safe_host(url):
    try:
        host = urlparse(url).netloc.lower().split(":", 1)[0]
    except Exception:
        return False
    return any(host == suffix or host.endswith("." + suffix) for suffix in SAFE_HOST_SUFFIXES)


def safe_text(*parts):
    haystack = " ".join(str(part or "") for part in parts).lower()
    return not any(term in haystack for term in BLOCKED_TERMS)


def upscale_amazon_url(url):
    return re.sub(
        r"\._V1_[^?]*?(?=\.(?:jpe?g|png|webp)(?:\?|$))",
        "._V1_QL85_UX1600_",
        url,
        flags=re.I,
    )


def extract_image_candidates(page_url, text, source_type, context):
    soup = BeautifulSoup(text, "html.parser")
    rows = []
    seen = set()
    for tag in soup.find_all(["img", "source", "meta", "a"]):
        alt = " ".join(
            value for value in [
                tag.get("alt", ""),
                tag.get("title", ""),
                tag.get("aria-label", ""),
                tag.get("content", "") if tag.name == "meta" else "",
            ] if value
        )
        values = []
        for attr in ["src", "data-src", "data-lazy-src", "data-original", "href", "content"]:
            if tag.get(attr):
                values.append(tag.get(attr))
        for attr in ["srcset", "data-srcset"]:
            if tag.get(attr):
                values.extend(piece.strip().split()[0] for piece in tag.get(attr).split(","))
        for raw in values:
            url = html.unescape(str(raw)).replace("\\/", "/")
            url = urljoin(page_url, url)
            if "media-amazon.com" in url:
                url = upscale_amazon_url(url)
            if url in seen or not safe_host(url) or not safe_text(url, alt, context):
                continue
            seen.add(url)
            rows.append({
                "url": url,
                "alt": alt,
                "context": context,
                "source_type": source_type,
                "source_page": page_url,
            })
    return rows


def imdb_title_id(film, log):
    key = re.sub(r"[^a-z0-9]+", "_", film["title"].lower()).strip("_")
    url = f"https://v2.sg.media-imdb.com/suggestion/x/{key}.json"
    try:
        payload = json.loads(fast_request(url))
    except Exception as exc:
        log.append(f"imdb_resolve_failed={type(exc).__name__}")
        return None
    wanted = h.clean_text(film["title"])
    best = None
    for row in payload.get("d", []):
        imdb_id = str(row.get("id", ""))
        if not imdb_id.startswith("tt"):
            continue
        title = h.clean_text(row.get("l", ""))
        year = row.get("y")
        title_score = len(set(wanted.split()) & set(title.split()))
        year_score = 3 if year == film.get("year") else 0
        score = title_score + year_score
        if best is None or score > best[0]:
            best = (score, imdb_id, row.get("l", ""), year)
    if best:
        log.append(f"imdb_title={best[1]} score={best[0]} label={best[2]} year={best[3]}")
        return best[1]
    return None


def imdb_candidates(film, log):
    imdb_id = imdb_title_id(film, log)
    if not imdb_id:
        return []
    rows = []
    for page in range(1, 6):
        url = f"https://www.imdb.com/title/{imdb_id}/mediaindex/?contentTypes=still_frame&page={page}"
        try:
            text = fast_request(url, headers={"Accept-Language": "en-US,en;q=0.9"})
            found = extract_image_candidates(
                url,
                text,
                "trusted_imdb_still",
                f"IMDb still-frame gallery for {film['title']} ({film['year']})",
            )
            rows.extend(found)
            if not found or len(rows) >= 100:
                break
        except Exception as exc:
            log.append(f"imdb_page_failed={page} {type(exc).__name__}")
            break
    log.append(f"imdb_image_candidates={len(rows)}")
    return rows


def tmdb_candidates(film, log):
    search_url = "https://www.themoviedb.org/search/movie?query=" + quote_plus(film["title"])
    try:
        text = fast_request(search_url, headers={"Accept-Language": "en-US,en;q=0.9"})
    except Exception as exc:
        log.append(f"tmdb_search_failed={type(exc).__name__}")
        return []
    soup = BeautifulSoup(text, "html.parser")
    candidates = []
    wanted_tokens = set(h.clean_text(film["title"]).split())
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "")
        if not re.match(r"^/movie/\d+", href):
            continue
        label = h.clean_text(anchor.get_text(" ", strip=True) + " " + href)
        score = len(wanted_tokens & set(label.split()))
        if str(film.get("year")) in label:
            score += 3
        candidates.append((score, urljoin(search_url, href.split("?", 1)[0])))
    if not candidates:
        return []
    _, movie_url = max(candidates, key=lambda item: item[0])
    gallery_url = movie_url.rstrip("/") + "/images/backdrops"
    try:
        gallery = fast_request(gallery_url, headers={"Accept-Language": "en-US,en;q=0.9"})
    except Exception as exc:
        log.append(f"tmdb_gallery_failed={type(exc).__name__}")
        return []
    rows = extract_image_candidates(
        gallery_url,
        gallery,
        "trusted_tmdb_backdrop",
        f"TMDB backdrop gallery for {film['title']} ({film['year']})",
    )
    log.append(f"tmdb_image_candidates={len(rows)} page={gallery_url}")
    return rows


def bing_image_candidates(query, context, log, limit=35):
    url = "https://www.bing.com/images/search?q=" + quote_plus(query) + "&form=HDRSC2&first=1"
    try:
        text = fast_request(url, headers={"Accept-Language": "en-US,en;q=0.9"})
    except Exception as exc:
        log.append(f"bing_failed={type(exc).__name__} query={query}")
        return []
    soup = BeautifulSoup(text, "html.parser")
    rows = []
    seen = set()
    for tag in soup.select("a.iusc"):
        raw = html.unescape(tag.get("m", ""))
        if not raw:
            continue
        try:
            item = json.loads(raw)
        except Exception:
            continue
        image_url = item.get("murl") or item.get("turl")
        source_page = item.get("purl") or url
        title = item.get("t") or item.get("desc") or ""
        if not image_url or image_url in seen:
            continue
        if "media-amazon.com" in image_url:
            image_url = upscale_amazon_url(image_url)
        if not safe_host(image_url) or not safe_text(image_url, source_page, title, query):
            continue
        seen.add(image_url)
        rows.append({
            "url": image_url,
            "alt": title,
            "context": context,
            "source_type": "trusted_exact_search",
            "source_page": source_page,
            "query": query,
        })
        if len(rows) >= limit:
            break
    log.append(f"bing_candidates={len(rows)} query={query}")
    return rows


def download_candidates(candidates, film_dir, log, prefix, cap):
    output = []
    seen_urls = set()
    folder = film_dir / "_TRUSTED_POOL"
    folder.mkdir(parents=True, exist_ok=True)
    for item in candidates:
        url = item.get("url")
        if not url or url in seen_urls or not safe_host(url):
            continue
        seen_urls.add(url)
        try:
            data = fast_request(
                url,
                binary=True,
                headers={"Referer": item.get("source_page") or _CURRENT_PAGE_URL or "https://www.imdb.com/"},
            )
            if len(data) < 18000:
                continue
            destination = folder / f"{prefix}_{len(output):03d}.jpg"
            metadata = h.normalize_image(data, destination)
            if not metadata:
                continue
            if metadata["brightness"] < 8 or metadata["brightness"] > 247 or metadata["quality"] < 3.5:
                destination.unlink(missing_ok=True)
                continue
            metadata.update({
                "url": url,
                "alt": item.get("alt", ""),
                "context": item.get("context", ""),
                "source_type": item.get("source_type", "trusted_gallery"),
                "source_page": item.get("source_page"),
                "query": item.get("query"),
            })
            output.append(metadata)
            if len(output) >= cap:
                break
        except Exception:
            continue
    log.append(f"trusted_downloaded={len(output)} prefix={prefix}")
    return output


def trusted_gallery_pool(film, film_dir, log):
    base_candidates = imdb_candidates(film, log) + tmdb_candidates(film, log)
    base = download_candidates(base_candidates, film_dir, log, "gallery", 110)
    if len(base) < 45:
        queries = [
            f'"{film["title"]}" {film["year"]} animated movie still frame',
            f'"{film["title"]}" official animation still',
        ]
        extra_candidates = []
        for query in queries:
            extra_candidates.extend(
                bing_image_candidates(
                    query,
                    f"Exact-title trusted image search for {film['title']} ({film['year']})",
                    log,
                    limit=40,
                )
            )
        base.extend(download_candidates(extra_candidates, film_dir, log, "style_search", 55))

    character_pool = []
    for index, character in enumerate(film.get("characters", [])):
        query = f'"{film["title"]}" "{character}" animated character still'
        rows = bing_image_candidates(
            query,
            f"Character reference: {character} in {film['title']} ({film['year']})",
            log,
            limit=12,
        )
        for row in rows:
            row["alt"] = f"{character} {row.get('alt', '')}".strip()
            row["context"] = f"{character} — exact character search for {film['title']}"
        character_pool.extend(download_candidates(rows, film_dir, log, f"char_{index:02d}", 6))
        time.sleep(0.15)

    combined = h.dedupe(base + character_pool, threshold=5, cap=240)
    log.append(f"trusted_unique_pool={len(combined)}")
    return combined


def enhanced_site_pool(page_url, text, film_dir, log):
    official = _ORIGINAL_SITE_POOL(page_url, text, film_dir, log)
    trusted = trusted_gallery_pool(_CURRENT_FILM, film_dir, log)
    return official + trusted


h.download_site_pool = enhanced_site_pool


def fast_video_urls(page_url, text):
    urls = _ORIGINAL_EXTRACT_VIDEO_URLS(page_url, text)
    direct = []
    for url in urls:
        low = url.lower()
        if url == page_url:
            continue
        if any(token in low for token in ["player.vimeo.com", "vimeo.com", "youtube.com/watch", "youtu.be/", ".mp4", ".m3u8"]):
            direct.append(url)
    return list(dict.fromkeys(direct))[:5]


h.extract_video_urls = fast_video_urls


def vimeo_id(url):
    match = re.search(r"vimeo\.com/(?:video/)?(\d{6,12})", url)
    return match.group(1) if match else None


def youtube_id(url):
    match = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})", url)
    return match.group(1) if match else None


def stream_to_file(url, destination, headers=None):
    merged = dict(h.SESSION.headers)
    if headers:
        merged.update(headers)
    with h.SESSION.get(url, stream=True, timeout=90, allow_redirects=True, headers=merged) as response:
        response.raise_for_status()
        with destination.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 512):
                if chunk:
                    handle.write(chunk)
    return destination.exists() and destination.stat().st_size > 200000


def direct_vimeo_download(url, out_dir, stem, log):
    identifier = vimeo_id(url)
    if not identifier:
        return None
    referer = _CURRENT_PAGE_URL or "https://www.sonypicturesanimation.com/"
    embed_url = f"https://player.vimeo.com/video/{identifier}?dnt=1"
    headers = {
        "Referer": referer,
        "Origin": "https://www.sonypicturesanimation.com",
        "Accept-Language": "en-US,en;q=0.9",
    }
    config_urls = []
    try:
        player_html = fast_request(embed_url, headers=headers)
        for pattern in [r'"config_url":"([^"]+)"', r"config_url\s*[:=]\s*['\"]([^'\"]+)"]:
            for found in re.findall(pattern, player_html):
                config_urls.append(html.unescape(found).replace("\\/", "/"))
    except Exception as exc:
        log.append(f"vimeo_embed_failed={identifier} {type(exc).__name__}")
    config_urls.extend([
        f"https://player.vimeo.com/video/{identifier}/config",
        f"https://player.vimeo.com/video/{identifier}/config?autopause=0",
    ])
    config = None
    for config_url in dict.fromkeys(config_urls):
        try:
            raw = fast_request(config_url, headers={**headers, "Referer": embed_url})
            config = json.loads(raw)
            break
        except Exception:
            continue
    if not config:
        log.append(f"vimeo_config_unavailable={identifier}")
        return None
    files = config.get("request", {}).get("files", {})
    progressive = files.get("progressive") or []
    progressive = [row for row in progressive if row.get("url")]
    progressive.sort(key=lambda row: (row.get("height") or 0, row.get("width") or 0), reverse=True)
    for row in progressive:
        if (row.get("height") or 0) > 1080:
            continue
        destination = out_dir / f"{stem}.mp4"
        try:
            if stream_to_file(row["url"], destination, headers={"Referer": embed_url}):
                log.append(f"vimeo_progressive_downloaded={identifier} height={row.get('height')}")
                return destination
        except Exception:
            destination.unlink(missing_ok=True)
    hls = files.get("hls") or {}
    cdns = hls.get("cdns") or {}
    hls_url = None
    if hls.get("url"):
        hls_url = hls.get("url")
    elif cdns:
        default_cdn = hls.get("default_cdn")
        if default_cdn in cdns:
            hls_url = cdns[default_cdn].get("url")
        if not hls_url:
            hls_url = next((row.get("url") for row in cdns.values() if row.get("url")), None)
    if hls_url:
        destination = out_dir / f"{stem}.mp4"
        header_blob = f"Referer: {embed_url}\r\nUser-Agent: Mozilla/5.0\r\n"
        command = [
            "ffmpeg", "-loglevel", "error", "-headers", header_blob,
            "-i", hls_url, "-c", "copy", "-t", "00:06:00", str(destination),
        ]
        try:
            process = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
            if destination.exists() and destination.stat().st_size > 200000:
                log.append(f"vimeo_hls_downloaded={identifier}")
                return destination
            log.append(f"vimeo_hls_failed={identifier} {process.stderr[-160:]}")
        except Exception as exc:
            log.append(f"vimeo_hls_error={identifier} {type(exc).__name__}")
    return None


def fast_download_video(url, out_dir, stem, log):
    out_dir.mkdir(parents=True, exist_ok=True)
    if vimeo_id(url):
        direct = direct_vimeo_download(url, out_dir, stem, log)
        if direct:
            return direct
    template = str(out_dir / f"{stem}.%(ext)s")
    command = [
        "yt-dlp", "--no-playlist", "--no-warnings", "--sleep-requests", "1",
        "--referer", _CURRENT_PAGE_URL or "https://www.sonypicturesanimation.com/",
        "-f", "bv*[height<=720]+ba/b[height<=720]",
        "--merge-output-format", "mp4", "--max-filesize", "300M",
        "-o", template,
    ]
    if youtube_id(url):
        command.extend(["--extractor-args", "youtube:player_client=tv,web_safari"])
    if vimeo_id(url):
        command.extend(["--extractor-args", "vimeo:client=web"])
    command.append(url)
    try:
        process = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=210)
    except subprocess.TimeoutExpired:
        log.append(f"video_download_timeout={url}")
        return None
    for path in out_dir.glob(f"{stem}.*"):
        if path.suffix.lower() in {".mp4", ".webm", ".mkv", ".mov"} and path.stat().st_size > 200000:
            log.append(f"official_video_downloaded={url}")
            return path
    log.append(f"video_download_failed={url} {process.stderr[-220:]}")
    return None


h.download_video = fast_download_video


def youtube_thumbnail_frames(urls, film_dir, log):
    frames = []
    seen = set()
    for url in urls:
        identifier = youtube_id(url)
        if not identifier or identifier in seen:
            continue
        seen.add(identifier)
        for position, name in enumerate(["maxresdefault", "sddefault", "hqdefault", "0", "1", "2", "3"]):
            image_url = f"https://i.ytimg.com/vi/{identifier}/{name}.jpg"
            destination = film_dir / "_VIDEO_POOL" / f"yt_{identifier}_{position}.jpg"
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                data = fast_request(image_url, binary=True, headers={"Referer": "https://www.youtube.com/"})
                metadata = h.normalize_image(data, destination)
                if not metadata:
                    continue
                if metadata["brightness"] < 10 or metadata["brightness"] > 245 or metadata["quality"] < 3.5:
                    destination.unlink(missing_ok=True)
                    continue
                metadata.update({
                    "url": image_url,
                    "alt": f"official trailer thumbnail {identifier} {position}",
                    "context": f"Official trailer thumbnail for {_CURRENT_FILM['title']}",
                    "source_type": "official_video",
                })
                frames.append(metadata)
            except Exception:
                continue
    log.append(f"youtube_thumbnail_frames={len(frames)}")
    return frames


def fast_video_pool(film, page_url, text, film_dir, log, needed=True):
    if not needed:
        return []
    urls = h.extract_video_urls(page_url, text)
    fallback = h.search_official_video(film["title"], log)
    if fallback and fallback not in urls:
        urls.append(fallback)
    frames = youtube_thumbnail_frames(urls, film_dir, log)
    work = film_dir / "_VIDEO_DOWNLOADS"
    for index, url in enumerate(urls[:6]):
        try:
            video = h.download_video(url, work, f"video_{index}", log)
            if not video:
                continue
            extracted = h.extract_video_frames(video, url, film_dir, f"v{index}", log)
            frames.extend(extracted)
            if len(extracted) >= 24:
                break
        except Exception as exc:
            log.append(f"video_error={url} {type(exc).__name__}")
    shutil.rmtree(work, ignore_errors=True)
    return frames


h.video_pool = fast_video_pool


def process_with_context(film, root):
    global _CURRENT_FILM, _CURRENT_PAGE_URL
    _CURRENT_FILM = film
    try:
        page_url, _ = h.resolve_page(film, [])
        _CURRENT_PAGE_URL = page_url
    except Exception:
        _CURRENT_PAGE_URL = "https://www.sonypicturesanimation.com/"
    try:
        return _ORIGINAL_PROCESS(film, root)
    finally:
        _CURRENT_FILM = None
        _CURRENT_PAGE_URL = None


h.process = process_with_context

if __name__ == "__main__":
    h.main()
