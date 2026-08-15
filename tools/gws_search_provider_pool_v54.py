#!/usr/bin/env python3
"""Bounded multi-provider search for autonomous GWS verification.

HIGH semantics stay conservative: Yahoo is Bing-family and DDG HTML/Lite are one
DDG family. Runtime policy is fail-closed rather than retry-until-timeout: Bing
and DDG are queried concurrently, same-family transports are fallbacks, and a
blocked independent provider simply makes coverage fail instead of consuming
minutes of exponential backoff. Search stops as soon as the certificate's actual
minimum evidence requirements can be met.
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
    text = str(exc).lower(); name = type(exc).__name__.lower()
    return (
        "dns" in name or "name or service not known" in text or
        "nodename nor servname" in text or "no address associated with hostname" in text or
        ("name resolution" in text and "temporary failure" not in text)
    )


def provider_family(provider: str) -> str:
    p = str(provider)
    if p.startswith("ddg"):
        return "ddg"
    if p in {"bing", "yahoo"}:
        return "bing"
    return p


def provider_concurrency_plan(search_conc: int) -> dict[str, int]:
    c = max(1, int(search_conc))
    return {"bing": c, "yahoo": c, "ddg": 1}


def _transport_gate(provider: str) -> str:
    return "ddg" if str(provider).startswith("ddg") else str(provider)


def _urls(query: str):
    q = urllib.parse.quote_plus(query)
    return {
        "bing": f"https://www.bing.com/search?count=10&q={q}",
        "ddg_html": f"https://html.duckduckgo.com/html/?q={q}",
        "ddg_lite": f"https://lite.duckduckgo.com/lite/?q={q}",
        "yahoo": f"https://search.yahoo.com/search?p={q}",
    }


async def webcheck(rows, conc: int, search_conc: int):
    import aiohttp

    sem = asyncio.Semaphore(max(1, int(conc)))
    search_sems = {
        name: asyncio.Semaphore(limit)
        for name, limit in provider_concurrency_plan(search_conc).items()
    }
    timeout = aiohttp.ClientTimeout(total=12, connect=4, sock_read=7)
    headers = {"User-Agent": v2.UA, "Accept-Language": "fr-BE,fr;q=0.9,nl;q=0.8,en;q=0.7"}
    ans = {}

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as sess:
        async def get(url: str, *, is_search: bool = False, provider: str = "", attempts: int = 1):
            last = {}
            attempts = max(1, int(attempts))
            for attempt in range(attempts):
                try:
                    gate = search_sems[_transport_gate(provider)] if is_search else sem
                    async with gate:
                        if is_search:
                            if _transport_gate(provider) == "ddg":
                                await asyncio.sleep(random.uniform(.20, .45))
                            else:
                                await asyncio.sleep(random.uniform(.05, .15))
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
                if attempt + 1 < attempts:
                    await asyncio.sleep(.20 + random.uniform(.05, .20))
            return last or {"ok": False, "status": 0, "error": "UNKNOWN"}

        def parsed_health(provider: str, resp: dict, url: str):
            http_ok = bool(
                resp.get("ok") and 200 <= int(resp.get("status") or 999) < 300
                and not resp.get("blocked")
            )
            parsed = bool(http_ok and _parsed(provider, resp.get("body", "")))
            links = v4.hrefs(resp.get("body", ""), resp.get("url", url), 24) if parsed else []
            h = {
                "provider": provider,
                "provider_family": provider_family(provider),
                "http_ok": http_ok,
                "parsed": parsed,
                "status": resp.get("status"),
                "blocked": bool(resp.get("blocked")),
                "error": resp.get("error"),
                "external_domains": len({v2.host(x) for x in links}),
            }
            return parsed, links, h

        async def search_one_query(sq: str):
            urls = _urls(sq)
            # Independent families first, concurrently. No exponential retry storm.
            bing_resp, ddg_resp = await asyncio.gather(
                get(urls["bing"], is_search=True, provider="bing", attempts=2),
                get(urls["ddg_html"], is_search=True, provider="ddg_html", attempts=1),
            )
            bp, blinks, bh = parsed_health("bing", bing_resp, urls["bing"])
            dp, dlinks, dh = parsed_health("ddg_html", ddg_resp, urls["ddg_html"])
            health = [bh, dh]
            parsed_names = {p for p, ok in (("bing", bp), ("ddg_html", dp)) if ok}
            links = list(blinks) + list(dlinks)

            fallback_tasks = []
            fallback_names = []
            if not bp:
                fallback_names.append("yahoo")
                fallback_tasks.append(get(urls["yahoo"], is_search=True, provider="yahoo", attempts=1))
            if not dp:
                fallback_names.append("ddg_lite")
                fallback_tasks.append(get(urls["ddg_lite"], is_search=True, provider="ddg_lite", attempts=1))
            if fallback_tasks:
                for provider, resp in zip(fallback_names, await asyncio.gather(*fallback_tasks)):
                    pp, plinks, ph = parsed_health(provider, resp, urls[provider])
                    health.append(ph)
                    if pp:
                        parsed_names.add(provider)
                        links.extend(plinks)

            families = {provider_family(p) for p in parsed_names}
            return parsed_names, families, links, health

        async def one(c):
            ev = {
                "search_queries": 0, "search_usable_queries": 0, "search_health": [],
                "search_candidates": [], "healthy_providers": [], "direct_checked": 0,
                "direct_health": [], "owned": "", "owned_identity": {}, "owned_via": "",
                "candidate_seeds": [],
            }
            seeds = v4.guesses(c)
            ev["candidate_seeds"] = seeds[:]
            seed_hosts = {v2.host(x) for x in seeds if v2.host(x) and not v2.platform(x)}
            queries = list(v4.search_queries(c))
            if c.get("_unresolved_challenge"):
                queries = queries[:3]

            for sq in queries:
                ev["search_queries"] += 1
                parsed_names, families, qlinks, qhealth = await search_one_query(sq)
                for fam in sorted(families):
                    if fam not in ev["healthy_providers"]:
                        ev["healthy_providers"].append(fam)
                if len(families) >= 2:
                    ev["search_usable_queries"] += 1

                qseen = {v2.host(u) for u in qlinks if v2.host(u)}
                ev["search_health"].append({
                    "query": sq,
                    "providers": qhealth,
                    "parsed_providers": sorted(parsed_names),
                    "parsed_families": sorted(families),
                    "external_domains": len(qseen),
                })
                existing = {v2.host(x) for x in ev["search_candidates"] if v2.host(x)}
                for u in qlinks:
                    h = v2.host(u)
                    if h and h not in existing:
                        ev["search_candidates"].append(u)
                        existing.add(h)

                # Stop exactly when the downstream certificate can already satisfy
                # its search + direct-domain minimum. The old >=16 requirement was
                # expensive over-collection, not a HIGH gate.
                direct_pool = seed_hosts | existing
                if ev["search_usable_queries"] >= 2 and len(direct_pool) >= 5:
                    break

            cand = seeds + ev["search_candidates"]
            seen = set()
            direct_cap = 12 if c.get("_unresolved_challenge") else 20
            for u in cand:
                h = v2.host(u)
                if not h or h in seen or v2.platform(u):
                    continue
                seen.add(h)
                resp = await get(u, attempts=2)
                ev["direct_checked"] += 1
                dh = {
                    "seed": u, "final": resp.get("url", u), "status": resp.get("status"),
                    "ok": bool(resp.get("ok")), "error": resp.get("error"),
                    "dns_negative": bool(resp.get("dns_negative")),
                }
                if resp.get("ok") and not v4._dead(int(resp.get("status") or 999), resp.get("body", "")):
                    ide = v4.identity(c, resp.get("body", ""), resp.get("url", u))
                    dh["identity"] = ide
                    if ide["matched"] and not v2.platform(resp.get("url", u)):
                        ev["owned"] = resp.get("url", u)
                        ev["owned_identity"] = ide
                        ev["owned_via"] = "prior_or_email_domain" if u in seeds else "persistent_search"
                        ev["direct_health"].append(dh)
                        break
                ev["direct_health"].append(dh)
                if ev["direct_checked"] >= direct_cap:
                    break
            return int(c["r"]), ev

        results = await asyncio.gather(*(one(c) for c in rows))
        ans.update(results)
    return ans
