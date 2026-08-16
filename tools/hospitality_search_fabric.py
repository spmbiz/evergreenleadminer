#!/usr/bin/env python3
from __future__ import annotations

import os
import time
from dataclasses import dataclass, asdict
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
    status: str = 'OK'
    error: str = ''

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def query_specs(account: dict[str, Any], max_queries: int = 3) -> list[tuple[str, str]]:
    name = str(account.get('name') or '').strip()
    city = str(account.get('city') or account.get('region') or '').strip()
    phone = str(account.get('public_phone') or '').strip()
    raw = account.get('raw') or {}
    if not name:
        return []
    place = f' {city}' if city else ''
    specs: list[tuple[str, str]] = [('identity', f'"{name}"{place} official website')]
    if not (account.get('public_email') or raw.get('contact_page')):
        specs.append(('contact', f'"{name}"{place} contact email'))
    if not (account.get('instagram') or raw.get('instagram')):
        specs.append(('instagram', f'site:instagram.com "{name}"{place}'))
    if not raw.get('facebook'):
        specs.append(('facebook', f'site:facebook.com "{name}"{place}'))
    if phone:
        specs.append(('phone_identity', f'"{phone}" "{name}"'))
    return specs[: max(0, max_queries)]


class SearchFabric:
    def __init__(self, config: dict[str, Any]):
        self.config = config or {}
        self.timeout = float(self.config.get('timeout_seconds') or 15)
        self.max_results = int(self.config.get('max_results') or 5)
        self.engines = str(self.config.get('openserp_engines') or 'google,bing,duckduckgo,ecosia')
        self.openserp_url = (os.environ.get('OPENSERP_URL') or self.config.get('openserp_url') or '').rstrip('/')
        self.searxng_url = (os.environ.get('SEARXNG_URL') or self.config.get('searxng_url') or '').rstrip('/')
        self.ddgs_enabled = bool(self.config.get('ddgs_enabled', True))
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'AIProd-Hospitality-Resolver/2.0'})

    def _openserp(self, family: str, query: str) -> tuple[list[SearchResult], dict[str, Any]]:
        if not self.openserp_url:
            return [], {'provider': 'openserp', 'query_family': family, 'status': 'DISABLED', 'results': 0}
        url = (
            f'{self.openserp_url}/mega/search?text={quote_plus(query)}'
            f'&engines={quote_plus(self.engines)}&mode=balanced&dedupe=true&merge=true&limit={self.max_results}'
        )
        try:
            r = self.session.get(url, timeout=self.timeout)
            if r.status_code == 503:
                return [], {'provider': 'openserp', 'query_family': family, 'status': 'BLOCKED_OR_CAPTCHA', 'results': 0, 'http_status': 503}
            r.raise_for_status()
            data = r.json()
            items = data.get('results') or data.get('organic') or []
            out = []
            for i, item in enumerate(items[: self.max_results], 1):
                if not isinstance(item, dict):
                    continue
                target = str(item.get('url') or item.get('link') or '').strip()
                if not target:
                    continue
                out.append(SearchResult('openserp', family, query, i, str(item.get('title') or ''), target, str(item.get('snippet') or item.get('description') or '')))
            return out, {'provider': 'openserp', 'query_family': family, 'status': 'OK', 'results': len(out)}
        except Exception as exc:
            return [], {'provider': 'openserp', 'query_family': family, 'status': 'ERROR', 'results': 0, 'error': type(exc).__name__}

    def _ddgs(self, family: str, query: str) -> tuple[list[SearchResult], dict[str, Any]]:
        if not self.ddgs_enabled:
            return [], {'provider': 'ddgs', 'query_family': family, 'status': 'DISABLED', 'results': 0}
        try:
            from ddgs import DDGS  # type: ignore
            items = list(DDGS().text(query, max_results=self.max_results) or [])
            out = []
            for i, item in enumerate(items[: self.max_results], 1):
                if not isinstance(item, dict):
                    continue
                target = str(item.get('href') or item.get('url') or '').strip()
                if not target:
                    continue
                out.append(SearchResult('ddgs', family, query, i, str(item.get('title') or ''), target, str(item.get('body') or item.get('snippet') or '')))
            return out, {'provider': 'ddgs', 'query_family': family, 'status': 'OK', 'results': len(out)}
        except Exception as exc:
            return [], {'provider': 'ddgs', 'query_family': family, 'status': 'ERROR', 'results': 0, 'error': type(exc).__name__}

    def _searxng(self, family: str, query: str) -> tuple[list[SearchResult], dict[str, Any]]:
        if not self.searxng_url:
            return [], {'provider': 'searxng', 'query_family': family, 'status': 'DISABLED', 'results': 0}
        try:
            r = self.session.get(f'{self.searxng_url}/search', params={'q': query, 'format': 'json'}, timeout=self.timeout)
            r.raise_for_status()
            data = r.json()
            out = []
            for i, item in enumerate((data.get('results') or [])[: self.max_results], 1):
                target = str(item.get('url') or '').strip()
                if not target:
                    continue
                out.append(SearchResult('searxng', family, query, i, str(item.get('title') or ''), target, str(item.get('content') or '')))
            return out, {'provider': 'searxng', 'query_family': family, 'status': 'OK', 'results': len(out)}
        except Exception as exc:
            return [], {'provider': 'searxng', 'query_family': family, 'status': 'ERROR', 'results': 0, 'error': type(exc).__name__}

    def search(self, family: str, query: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        all_results: list[SearchResult] = []
        events: list[dict[str, Any]] = []
        providers = [self._openserp, self._ddgs, self._searxng]
        seen_urls: set[str] = set()
        for fn in providers:
            results, event = fn(family, query)
            events.append(event)
            for item in results:
                if item.url in seen_urls:
                    continue
                seen_urls.add(item.url)
                all_results.append(item)
            if results and self.config.get('stop_after_first_successful_provider', False):
                break
            delay = float(self.config.get('provider_delay_seconds') or 0)
            if delay:
                time.sleep(delay)
        return [r.to_dict() for r in all_results[: self.max_results * 3]], events
