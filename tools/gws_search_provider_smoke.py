#!/usr/bin/env python3
from __future__ import annotations

import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151 Safari/537.36"
QUERY = "Brussels bakery official website"


def title(body: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
    return html.unescape(re.sub(r"\s+", " ", m.group(1)).strip())[:160] if m else ""


def markers(name: str, body: str) -> dict:
    low = body.lower()
    checks = {
        "google": ["/url?q=", "data-snhf", "id=\"search\"", "class=\"g\""],
        "bing": ["b_results", "b_algo"],
        "ddg_html": ["result__a", "result__body", "results_links"],
        "ddg_lite": ["result-link", "result-snippet", "links_main"],
        "brave": ["snippet", "result-header", "data-type=\"web\"", "search-result"],
        "mojeek": ["results-standard", "ob", "class=\"title\""],
        "ecosia": ["result-title", "result__title", "mainline"],
        "yahoo": ["compTitle", "algo-sr", "searchCenterMiddle"],
    }
    return {m: (m.lower() in low) for m in checks.get(name, [])}


def main():
    q = urllib.parse.quote_plus(QUERY)
    providers = {
        "google": f"https://www.google.com/search?hl=en&num=10&filter=0&q={q}",
        "bing": f"https://www.bing.com/search?count=10&q={q}",
        "ddg_html": f"https://html.duckduckgo.com/html/?q={q}",
        "ddg_lite": f"https://lite.duckduckgo.com/lite/?q={q}",
        "brave": f"https://search.brave.com/search?q={q}&source=web",
        "mojeek": f"https://www.mojeek.com/search?q={q}",
        "ecosia": f"https://www.ecosia.org/search?q={q}",
        "yahoo": f"https://search.yahoo.com/search?p={q}",
    }
    out = []
    for name, url in providers.items():
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.8"})
        status = 0; body = ""; final = url; error = ""
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                status = int(r.status); final = r.geturl(); body = r.read(400000).decode(errors="ignore")
        except urllib.error.HTTPError as e:
            status = int(e.code); final = e.geturl(); body = e.read(400000).decode(errors="ignore"); error = f"HTTPError:{e.code}"
        except Exception as e:
            error = f"{type(e).__name__}:{e}"
        low = body.lower()
        rec = {
            "provider": name, "status": status, "bytes": len(body), "final_host": urllib.parse.urlparse(final).netloc,
            "title": title(body), "error": error,
            "captcha_like": any(x in low for x in ("captcha", "verify you are human", "unusual traffic", "challenge-platform")),
            "consent_like": any(x in low for x in ("before you continue", "consent.google", "privacy choices")),
            "markers": markers(name, body),
            "href_count": len(re.findall(r"href\s*=", body, re.I)),
        }
        out.append(rec)
        print("PROVIDER_SMOKE=" + json.dumps(rec, separators=(",", ":")))
    healthy = [x["provider"] for x in out if 200 <= x["status"] < 300 and not x["captcha_like"] and any(x["markers"].values())]
    print("PROVIDER_SMOKE_SUMMARY=" + json.dumps({"healthy_marker_providers": healthy, "count": len(healthy)}, separators=(",", ":")))


if __name__ == "__main__":
    main()
