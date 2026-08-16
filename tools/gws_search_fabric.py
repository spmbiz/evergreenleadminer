#!/usr/bin/env python3
from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import quote_plus

import requests


@dataclass
class SearchResult:
    provider: str
    query_family: str
    query_text: str
    rank: int
    title: str
    url: str
    snippet: str
    status: str = "OK"
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def query_specs(candidate: dict[str, Any], max_queries: int = 5) -> list[tuple[str, str]]:
    name = str(candidate.get("n") or candidate.get("name") or "").strip()
    postcode = str(candidate.get("p") or candidate.get("postcode") or "").strip()
    address = str(candidate.get("a") or candidate.get("address") or "").strip()
    phone = str(candidate.get("ph") or candidate.get("phone") or "").strip()
    alias = str(candidate.get("alias") or "").strip()
    if not name:
        return []
    out: list[tuple[str, str]] = []
    def add(family: str, query: str) -> None:
        query = " ".join(query.split()).strip()
        if query and query not in {q for _, q in out}:
            out.append((family, query))
    if postcode:
        add("identity_postcode", f'"{name}" {postcode}')
    if address:
        street = " ".join(address.split()[:8])
        add("identity_address", f'"{name}" "{street}"')
    if phone:
        add("phone_identity", f'"{phone}"')
    add("official_site", f'"{name}" Brussels official website')
    add("contact", f'"{name}" Brussels contact website')
    if alias and alias.lower() != name.lower():
        add("alias_identity", f'"{alias}" {postcode}'.strip())
    return out[:max(0, max_queries)]


class SearchFabric:
    def __init__(self, config: dict[str, Any]):
        self.config = config or {}
        self.timeout = float(self.config.get("timeout_seconds") or 15)
        self.max_results = int(self.config.get("max_results") or 8)
        self.engines = str(self.config.get("openserp_engines") or "google,bing,duckduckgo,ecosia,yandex")
        self.openserp_url = (os.environ.get("OPENSERP_URL") or self.config.get("openserp_url") or "").rstrip("/")
        self.searxng_url = (os.environ.get("SEARXNG_URL") or self.config.get("searxng_url") or "").rstrip("/")
        self.ddgs_enabled = bool(self.config.get("ddgs_enabled", True))
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "GWS-Search-Fabric/1.0"})

    def openserp_healthy(self) -> bool:
        if not self.openserp_url:
            return False
        try:
            return self.session.get(self.openserp_url + "/health", timeout=min(5, self.timeout)).status_code < 400
        except Exception:
            return False

    def _openserp(self, family: str, query: str) -> tuple[list[SearchResult], dict[str, Any]]:
        if not self.openserp_url:
            return [], {"provider":"openserp","query_family":family,"status":"DISABLED","results":0}
        url = (f"{self.openserp_url}/mega/search?text={quote_plus(query)}"
               f"&engines={quote_plus(self.engines)}&mode=balanced&dedupe=true&merge=true&limit={self.max_results}")
        try:
            r = self.session.get(url, timeout=self.timeout)
            if r.status_code in {403,429,503}:
                return [], {"provider":"openserp","query_family":family,"status":"BLOCKED_OR_CAPTCHA","results":0,"http_status":r.status_code}
            r.raise_for_status()
            data = r.json()
            items = data.get("results") or data.get("organic") or []
            out=[]
            for i,item in enumerate(items[:self.max_results],1):
                if not isinstance(item,dict): continue
                target=str(item.get("url") or item.get("link") or "").strip()
                if target:
                    out.append(SearchResult("openserp",family,query,i,str(item.get("title") or ""),target,str(item.get("snippet") or item.get("description") or "")))
            return out, {"provider":"openserp","query_family":family,"status":"OK","results":len(out),"meta":data.get("meta") or {}}
        except Exception as exc:
            return [], {"provider":"openserp","query_family":family,"status":"ERROR","results":0,"error":type(exc).__name__}

    def _ddgs(self, family: str, query: str) -> tuple[list[SearchResult], dict[str, Any]]:
        if not self.ddgs_enabled:
            return [], {"provider":"ddgs","query_family":family,"status":"DISABLED","results":0}
        try:
            from ddgs import DDGS  # type: ignore
            items=list(DDGS().text(query,max_results=self.max_results) or [])
            out=[]
            for i,item in enumerate(items[:self.max_results],1):
                if not isinstance(item,dict): continue
                target=str(item.get("href") or item.get("url") or "").strip()
                if target:
                    out.append(SearchResult("ddgs",family,query,i,str(item.get("title") or ""),target,str(item.get("body") or item.get("snippet") or "")))
            return out, {"provider":"ddgs","query_family":family,"status":"OK","results":len(out)}
        except Exception as exc:
            return [], {"provider":"ddgs","query_family":family,"status":"ERROR","results":0,"error":type(exc).__name__}

    def _searxng(self, family: str, query: str) -> tuple[list[SearchResult], dict[str, Any]]:
        if not self.searxng_url:
            return [], {"provider":"searxng","query_family":family,"status":"DISABLED","results":0}
        try:
            r=self.session.get(self.searxng_url+"/search",params={"q":query,"format":"json"},timeout=self.timeout)
            r.raise_for_status(); data=r.json(); out=[]
            for i,item in enumerate((data.get("results") or [])[:self.max_results],1):
                target=str(item.get("url") or "").strip()
                if target:
                    out.append(SearchResult("searxng",family,query,i,str(item.get("title") or ""),target,str(item.get("content") or "")))
            return out, {"provider":"searxng","query_family":family,"status":"OK","results":len(out)}
        except Exception as exc:
            return [], {"provider":"searxng","query_family":family,"status":"ERROR","results":0,"error":type(exc).__name__}

    def search(self, family: str, query: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        all_results=[]; events=[]; seen=set()
        for fn in (self._openserp,self._ddgs,self._searxng):
            results,event=fn(family,query); events.append(event)
            for item in results:
                if item.url not in seen:
                    seen.add(item.url); all_results.append(item)
            if results and self.config.get("stop_after_first_successful_provider",False):
                break
            delay=float(self.config.get("provider_delay_seconds") or 0)
            if delay: time.sleep(delay)
        return [x.to_dict() for x in all_results[:self.max_results*3]], events
