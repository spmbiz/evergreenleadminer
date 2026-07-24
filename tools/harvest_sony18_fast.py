import shutil
import subprocess
import time
from pathlib import Path

import tools.harvest_sony18 as h

# One film per job prevents an old embedded player from blocking an entire volume.
h.GROUP_SIZE = 1
h.TIMEOUT = 10


def fast_request(url, binary=False):
    last = None
    for attempt in range(2):
        try:
            r = h.SESSION.get(url, timeout=h.TIMEOUT, allow_redirects=True)
            r.raise_for_status()
            return r.content if binary else r.text
        except Exception as exc:
            last = exc
            time.sleep(0.8 + attempt)
    raise last


h.request = fast_request
_original_extract_video_urls = h.extract_video_urls


def fast_video_urls(page_url, text):
    urls = _original_extract_video_urls(page_url, text)
    direct = [u for u in urls if u != page_url and any(k in u.lower() for k in ["vimeo.com", "youtube.com", "youtu.be", ".mp4", ".m3u8"])]
    return direct[:3]


h.extract_video_urls = fast_video_urls


def fast_download_video(url, out_dir, stem, log):
    out_dir.mkdir(parents=True, exist_ok=True)
    template = str(out_dir / f"{stem}.%(ext)s")
    cmd = [
        "yt-dlp", "--no-playlist", "--no-warnings",
        "-f", "bv*[height<=720]+ba/b[height<=720]",
        "--merge-output-format", "mp4", "--max-filesize", "260M",
        "-o", template, url,
    ]
    try:
        proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=150)
    except subprocess.TimeoutExpired:
        log.append(f"video_download_timeout={url}")
        return None
    for p in out_dir.glob(f"{stem}.*"):
        if p.suffix.lower() in {".mp4", ".webm", ".mkv", ".mov"} and p.stat().st_size > 200000:
            log.append(f"official_video_downloaded={url}")
            return p
    log.append(f"video_download_failed={url} {proc.stderr[-180:]}")
    return None


h.download_video = fast_download_video


def fast_video_pool(film, page_url, text, film_dir, log, needed=True):
    if not needed:
        return []
    urls = h.extract_video_urls(page_url, text)
    fallback = h.search_official_video(film["title"], log)
    if fallback and fallback not in urls:
        urls.append(fallback)
    work = film_dir / "_VIDEO_DOWNLOADS"
    frames = []
    for idx, url in enumerate(urls[:4]):
        try:
            vid = h.download_video(url, work, f"video_{idx}", log)
            if not vid:
                continue
            frames.extend(h.extract_video_frames(vid, url, film_dir, f"v{idx}", log))
            break
        except Exception as exc:
            log.append(f"video_error={url} {type(exc).__name__}")
    shutil.rmtree(work, ignore_errors=True)
    return frames


h.video_pool = fast_video_pool

if __name__ == "__main__":
    h.main()
