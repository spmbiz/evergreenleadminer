#!/usr/bin/env python3
"""Search-provider pool for autonomous GWS verification.

Designed from live GitHub-runner smoke tests. It avoids making certification
depend on Google's JS/throttle surface, serializes search traffic per worker,
backs off on 202/429 responses, and uses multiple parseable public search
transports while preserving fail-closed coverage semantics.
"""
from __future__ import annotations

import asyncio
import random
import urllib.parse

import gws_legacy_deep_v2 as v2
import gws_legacy_deep_v4 as v4


def _parsed(provider: str, body: str) -> bool:
    low = (body or "").lower()
    if len(low) < 800:
        return False
    negative = (
        "did not match any documents", "no results", "aucun résultat", "geen resultaten",
        "we did not find results", "there are no results for",
    )
    if any(x in low for x in negative):
        return True
    if provider == "bing":
        return "b_results" in low or "b_algo" in low
    if provider == "ddg_html":
        return "result__a" in low or "result__body" in low or "results_links" in low
    if provider == "ddg_lite":
        return "result-link" in low or "result-snippet" in low
    if provider == "yahoo":
        return "comptitle" in low or "searchcentermiddle" in low
    return False


def _blocked(status: int, body: str) -> bool:
    low = (body or "").lower()
    return status in (202, 403, 429, 503) or any(x in low for x in (
        "unusual traffic", "captcha", "verify you are human", "challenge-platform",
        "detected unusual", "before you continue to google", "consent.google.com",
    ))


def _dns_negative(exc: BaseException) -> bool:
    # Keep this local so the provider module has no circular dependency on v53.
    text = str(exc).lower(); name = type(exc).__name__.lower()
    return (
        "dns" in name or "name or service not known" in text or
        "nodename nor servname" in text or "no address associated with hostname" in text or
        ("name resolution" in text and "temporary failure" not in text)
    )


async def webcheck(rows, conc: int, search_conc: int):
    import aiohttp

    sem = asyncio.Semaphore(max(1, int(conc)))
    # Critical: provider smoke was healthy alone, while 10 workers x concurrency=2
    # made DDG return 202. Serialize search requests inside each worker.
    ssem = asyncio.Semaphore(1)
    timeout = aiohttp.ClientTimeout(total=18, connect=5, sock_read=11)
    headers = {"User-Agent": v2.UA, "Accept-Language": "fr-BE,fr;q=0.9,nl;q=0.8,en;q=0.7"}
    ans = {}

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as sess:
        async def get(url: str, is_search: bool = False):
            attempts = 4 if is_search else 2
            last = {}
            for attempt in range(attempts):
                try:
                    async with (ssem if is_search else sem):
                        if is_search:
                            # Desynchronize independent GitHub workers/IP bursts.
                            await asyncio.sleep(random.uniform(.45, 1.15) + attempt * .35)
                        async with sess.get(url, allow_redirects=True, ssl=True) as r:
                            body = (await r.content.read(v2.MAXBODY)).decode(errors="ignore")
                            if _blocked(int(r.status), body):
                                last = {"ok": False, "status": int(r.status), "blocked": True, "url": str(r.url)}
                            else:
                                return {"ok": True, "status": int(r.status), "url": str(r.url), "body": body}
                except Exception as exc:
                    if not is_search and _dns_negative(exc):
                        return {"ok": False, "status": 404, "dns_negative": True, "error": ""}
                    last = {"ok": False, "status": 0, "error": type(exc).__name__, "error_detail": str(exc)[:160]}
                if is_search:
                    await asyncio.sleep((1.0 * (2 ** attempt)) + random.uniform(.1, .7))
                else:
                    await asyncio.sleep(.25 * (2 ** attempt))
            return last or {"ok": False, "status": 0, "error": "UNKNOWN"}

        def primary_providers(query: str):
            q = urllib.parse.quote_plus(query)
            return [
                ("bing", f"https://www.bing.com/search?count=10&q={q}"),
                ("ddg_html", f"https://html.duckduckgo.com/html/?q={q}"),
                ("yahoo", f"https://search.yahoo.com/search?p={q}"),
            ]

        def ddg_lite(query: str):
            q = urllib.parse.quote_plus(query)
            return "ddg_lite", f"https://lite.duckduckgo.com/lite/?q={q}"

        async def one(c):
            ev = {
                "search_queries": 0, "search_usable_queries": 0, "search_health": [],
                "search_candidates": [], "healthy_providers": [], "direct_checked": 0,
                "direct_health": [], "owned": "", "owned_identity": {}, "owned_via": "",
                "candidate_seeds": [],
            }
            seeds = v4.guesses(c); ev["candidate_seeds"] = seeds[:]
            for sq in v4.search_queries(c):
                ev["search_queries"] += 1
                qlinks, qseen, qhealth = [], set(), []
                parsed_names = set()
                ddg_ok = False
                for provider, url in primary_providers(sq):
                    resp = await get(url, is_search=True)
                    http_ok = bool(resp.get("ok") and 200 <= int(resp.get("status") or 999) < 300 and not resp.get("blocked"))
                    parsed = bool(http_ok and _parsed(provider, resp.get("body", "")))
                    if parsed:
                        parsed_names.add(provider)
                        if provider not in ev["healthy_providers"]:
                            ev["healthy_providers"].append(provider)
                        if provider == "ddg_html": ddg_ok = True
                    links = v4.hrefs(resp.get("body", ""), resp.get("url", url), 24) if parsed else []
                    qhealth.append({
                        "provider": provider, "http_ok": http_ok, "parsed": parsed,
                        "status": resp.get("status"), "blocked": bool(resp.get("blocked")),
                        "error": resp.get("error"), "external_domains": len({v2.host(x) for x in links}),
                    })
                    for u in links:
                        h = v2.host(u)
                        if h and h not in qseen:
                            qseen.add(h); qlinks.append(u)

                # DDG Lite is a transport fallback, not a second independent DDG vote.
                if not ddg_ok:
                    provider, url = ddg_lite(sq)
                    resp = await get(url, is_search=True)
                    http_ok = bool(resp.get("ok") and 200 <= int(resp.get("status") or 999) < 300 and not resp.get("blocked"))
                    parsed = bool(http_ok and _parsed(provider, resp.get("body", "")))
                    if parsed:
                        parsed_names.add("ddg")
                        if "ddg" not in ev["healthy_providers"]:
                            ev["healthy_providers"].append("ddg")
                    links = v4.hrefs(resp.get("body", ""), resp.get("url", url), 24) if parsed else []
                    qhealth.append({
                        "provider": provider, "http_ok": http_ok, "parsed": parsed,
                        "status": resp.get("status"), "blocked": bool(resp.get("blocked")),
                        "error": resp.get("error"), "external_domains": len({v2.host(x) for x in links}),
                    })
                    for u in links:
                        h = v2.host(u)
                        if h and h not in qseen:
                            qseen.add(h); qlinks.append(u)

                # Normalize DDG HTML/Lite into one provider family for coverage.
                families = set()
                for p in parsed_names:
                    families.add("ddg" if p.startswith("ddg") else p)
                if len(families) >= 2:
                    ev["search_usable_queries"] += 1
                ev["search_health"].append({
                    "query": sq, "providers": qhealth, "parsed_providers": sorted(parsed_names),
                    "parsed_families": sorted(families), "external_domains": len(qseen),
                })
                existing = {v2.host(x) for x in ev["search_candidates"]}
                for u in qlinks:
                    h = v2.host(u)
                    if h and h not in existing:
                        ev["search_candidates"].append(u); existing.add(h)
                if len(existing) >= 16 and ev["search_usable_queries"] >= 2:
                    break

            cand = seeds + ev["search_candidates"]
            seen = set()
            for u in cand:
                h = v2.host(u)
                if not h or h in seen or v2.platform(u):
                    continue
                seen.add(h)
                resp = await get(u)
                ev["direct_checked"] += 1
                dh = {
                    "seed": u, "final": resp.get("url", u), "status": resp.get("status"),
                    "ok": bool(resp.get("ok")), "error": resp.get("error"),
                    "dns_negative": bool(resp.get("dns_negative")),
                }
                if resp.get("ok") and not v4._dead(int(resp.get("status") or 999), resp.get("body", "")):
                    ide = v4.identity(c, resp.get("body", ""), resp.get("url", u)); dh["identity"] = ide
                    if ide["matched"] and not v2.platform(resp.get("url", u)):
                        ev["owned"] = resp.get("url", u); ev["owned_identity"] = ide
                        ev["owned_via"] = "prior_or_email_domain" if u in seeds else "persistent_search"
                        ev["direct_health"].append(dh); break
                ev["direct_health"].append(dh)
                if ev["direct_checked"] >= 20:
                    break
            return int(c["r"]), ev

        results = await asyncio.gather(*(one(c) for c in rows))
        ans.update(results)
    return ans
