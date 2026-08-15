#!/usr/bin/env python3
"""Tiered web evidence for autonomous GWS verification.

Broad rows use cheap Bing + deterministic direct-domain checks. Only candidates
that are already strong-current-identity eligible for HIGH are allowed onto the
independent-index path. For those rows Exa is the second evidence family; if its
API key/provider is unavailable, coverage fails closed and HIGH is impossible.

No public DDG/Yahoo dependency is used for strict certification: live GitHub
calibration showed those transports to be the dominant timeout/error source.
"""
from __future__ import annotations

import asyncio
import json
import os
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
    if p in {"bing", "yahoo"}:
        return "bing"
    if p == "exa":
        return "exa"
    return p


def provider_concurrency_plan(search_conc: int) -> dict[str, int]:
    c = max(1, int(search_conc))
    return {"bing": c, "yahoo": c, "exa": c}


def _transport_gate(provider: str) -> str:
    return str(provider)


def _urls(query: str):
    q = urllib.parse.quote_plus(query)
    return {
        "bing": f"https://www.bing.com/search?count=10&q={q}",
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
    exa_key = os.getenv("EXA_API_KEY", "").strip()
    ans = {}

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as sess:
        async def get(url: str, *, is_search: bool = False, provider: str = "", attempts: int = 1):
            last = {}
            for attempt in range(max(1, int(attempts))):
                try:
                    gate = search_sems[_transport_gate(provider)] if is_search else sem
                    async with gate:
                        if is_search:
                            await asyncio.sleep(random.uniform(.03, .12))
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
                if attempt + 1 < max(1, int(attempts)):
                    await asyncio.sleep(.15 + random.uniform(.03, .12))
            return last or {"ok": False, "status": 0, "error": "UNKNOWN"}

        async def exa_search(query: str):
            if not exa_key:
                return {
                    "ok": False, "status": 0, "error": "EXA_API_KEY_MISSING",
                    "parsed": False, "links": [],
                }
            payload = {
                "query": query,
                "type": "fast",
                "numResults": 10,
                "userLocation": "BE",
            }
            try:
                async with search_sems["exa"]:
                    async with sess.post(
                        "https://api.exa.ai/search",
                        headers={"x-api-key": exa_key, "content-type": "application/json", "accept": "application/json"},
                        json=payload,
                        ssl=True,
                    ) as r:
                        raw = await r.content.read(v2.MAXBODY)
                        status = int(r.status)
                        if status != 200:
                            return {"ok": False, "status": status, "error": f"EXA_HTTP_{status}", "parsed": False, "links": []}
                        try:
                            data = json.loads(raw.decode(errors="ignore"))
                        except Exception as exc:
                            return {"ok": False, "status": status, "error": type(exc).__name__, "parsed": False, "links": []}
                        results = data.get("results")
                        if not isinstance(results, list):
                            return {"ok": False, "status": status, "error": "EXA_RESULTS_SCHEMA", "parsed": False, "links": []}
                        links = []
                        seen = set()
                        for item in results:
                            u = str((item or {}).get("url") or "").strip()
                            h = v2.host(u)
                            if u.startswith("http") and h and h not in seen and not v2.platform(u):
                                seen.add(h); links.append(u)
                        # A valid empty result set is still a parsed negative search observation.
                        return {"ok": True, "status": status, "error": "", "parsed": True, "links": links}
            except Exception as exc:
                return {"ok": False, "status": 0, "error": type(exc).__name__, "parsed": False, "links": []}

        def parsed_html_health(provider: str, resp: dict, url: str):
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

        async def search_one_query(sq: str, strict_high: bool):
            urls = _urls(sq)
            if strict_high:
                bing_resp, exa_resp = await asyncio.gather(
                    get(urls["bing"], is_search=True, provider="bing", attempts=2),
                    exa_search(sq),
                )
                bp, blinks, bh = parsed_html_health("bing", bing_resp, urls["bing"])
                ep = bool(exa_resp.get("parsed"))
                elinks = list(exa_resp.get("links") or []) if ep else []
                eh = {
                    "provider": "exa",
                    "provider_family": "exa",
                    "http_ok": bool(exa_resp.get("ok")),
                    "parsed": ep,
                    "status": exa_resp.get("status"),
                    "blocked": False,
                    "error": exa_resp.get("error"),
                    "external_domains": len({v2.host(x) for x in elinks if v2.host(x)}),
                }
                parsed_names = {p for p, ok in (("bing", bp), ("exa", ep)) if ok}
                families = {provider_family(p) for p in parsed_names}
                return parsed_names, families, list(blinks) + elinks, [bh, eh]

            # HIGH-ineligible rows get a cheap bounded discovery challenge only.
            bing_resp = await get(urls["bing"], is_search=True, provider="bing", attempts=2)
            bp, blinks, bh = parsed_html_health("bing", bing_resp, urls["bing"])
            health = [bh]
            parsed_names = {"bing"} if bp else set()
            links = list(blinks)
            if not bp:
                yahoo_resp = await get(urls["yahoo"], is_search=True, provider="yahoo", attempts=1)
                yp, ylinks, yh = parsed_html_health("yahoo", yahoo_resp, urls["yahoo"])
                health.append(yh)
                if yp:
                    parsed_names.add("yahoo")
                    links.extend(ylinks)
            families = {provider_family(p) for p in parsed_names}
            return parsed_names, families, links, health

        async def one(c):
            ev = {
                "search_queries": 0, "search_usable_queries": 0, "search_health": [],
                "search_candidates": [], "healthy_providers": [], "direct_checked": 0,
                "direct_health": [], "owned": "", "owned_identity": {}, "owned_via": "",
                "candidate_seeds": [], "strict_high_path": bool(c.get("_strict_high_candidate")),
            }
            strict_high = bool(c.get("_strict_high_candidate"))
            seeds = v4.guesses(c)
            ev["candidate_seeds"] = seeds[:]
            seed_hosts = {v2.host(x) for x in seeds if v2.host(x) and not v2.platform(x)}
            queries = list(v4.search_queries(c))
            # Bound cost and runtime deterministically. HIGH requires two usable
            # formulations per pass, so two is the exact strict minimum.
            queries = queries[:2] if strict_high else queries[:3]

            for sq in queries:
                ev["search_queries"] += 1
                parsed_names, families, qlinks, qhealth = await search_one_query(sq, strict_high)
                for fam in sorted(families):
                    if fam not in ev["healthy_providers"]:
                        ev["healthy_providers"].append(fam)
                if strict_high:
                    if len(families) >= 2:
                        ev["search_usable_queries"] += 1
                elif families:
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

                direct_pool = seed_hosts | existing
                if strict_high and ev["search_usable_queries"] >= 2 and len(direct_pool) >= 5:
                    break
                if not strict_high and ev["search_queries"] >= 2 and len(direct_pool) >= 5:
                    break

            cand = seeds + ev["search_candidates"]
            seen = set()
            direct_cap = 20 if strict_high else 12
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
