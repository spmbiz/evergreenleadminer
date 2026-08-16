#!/usr/bin/env python3
from __future__ import annotations

import gzip
import html
import ipaddress
import re
import socket
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

EMAIL_RE = re.compile(r'(?<![\w.+-])([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})(?![\w.-])', re.I)
HREF_RE = re.compile(r'''href\s*=\s*["']([^"']+)["']''', re.I)
SITEMAP_RE = re.compile(r'^\s*Sitemap:\s*(\S+)\s*$', re.I | re.M)

MULTI_SUFFIXES = {
    'co.uk','org.uk','me.uk','ltd.uk','plc.uk','net.uk','com.au','net.au','org.au','id.au','asn.au',
    'co.nz','net.nz','org.nz','com.br','net.br','org.br','com.mx','com.ar','com.co','com.pe','com.ec',
    'co.za','org.za','net.za','com.sg','com.hk','co.jp','ne.jp','com.tr','co.il','com.my','co.th','com.ph'
}
PROPERTY_HINTS = (
    '/villa', '/villas', '/property', '/properties', '/rental', '/rentals', '/listing', '/listings',
    '/holiday-home', '/holiday-homes', '/vacation-rental', '/vacation-rentals', '/accommodation/',
    '/apartment/', '/apartments/', '/chalet/', '/chalets/', '/cabin/', '/cabins/', '/home/', '/homes/'
)
UTILITY_HINTS = ('/blog/', '/news/', '/contact', '/about', '/privacy', '/terms', '/faq', '/category/', '/tag/', '/author/')
PMS_PATTERNS = {
    'guesty': ('guesty.com', 'guesty.cloud', 'guesty.co'),
    'hostaway': ('hostaway.com', 'hostaway-platform'),
    'lodgify': ('lodgify.com', 'lodgify'),
    'hostfully': ('hostfully.com', 'hostfully'),
    'ownerrez': ('ownerrez.com', 'ownerrez'),
    'beds24': ('beds24.com', 'beds24'),
    'cloudbeds': ('cloudbeds.com', 'cloudbeds'),
    'smoobu': ('smoobu.com', 'smoobu'),
    'hospitable': ('hospitable.com', 'hospitable'),
    'rentals_united': ('rentalsunited.com', 'rentals united'),
    'bookingautomation': ('bookingautomation.com', 'bookingautomation'),
}


def registrable_domain(host: str) -> str:
    h = (host or '').lower().strip('.')
    if h.startswith('www.'):
        h = h[4:]
    parts = h.split('.')
    if len(parts) < 2:
        return h
    last2 = '.'.join(parts[-2:])
    if last2 in MULTI_SUFFIXES and len(parts) >= 3:
        return '.'.join(parts[-3:])
    return last2


def public_host(host: str) -> bool:
    h = (host or '').strip().lower().rstrip('.')
    if not h or h in {'localhost', 'localhost.localdomain'}:
        return False
    try:
        infos = socket.getaddrinfo(h, None, type=socket.SOCK_STREAM)
    except Exception:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except Exception:
            return False
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            return False
    return True


def same_site(url: str, root_domain: str) -> bool:
    host = (urlparse(url).hostname or '').lower()
    return bool(host and registrable_domain(host) == root_domain)


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int
    text: str
    content_type: str
    error: str = ''


def bounded_get(session: requests.Session, url: str, root_domain: str, timeout: float, max_bytes: int) -> FetchResult:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or not public_host(parsed.hostname):
            return FetchResult(url, url, 0, '', '', 'UNSAFE_OR_UNRESOLVABLE_HOST')
        if registrable_domain(parsed.hostname) != root_domain:
            return FetchResult(url, url, 0, '', '', 'CROSS_DOMAIN_BLOCKED')
        with session.get(url, timeout=timeout, allow_redirects=True, stream=True) as r:
            final = r.url
            if not same_site(final, root_domain):
                return FetchResult(url, final, r.status_code, '', r.headers.get('content-type',''), 'CROSS_DOMAIN_REDIRECT_BLOCKED')
            data = bytearray()
            for chunk in r.iter_content(65536):
                if not chunk:
                    continue
                remaining = max_bytes - len(data)
                if remaining <= 0:
                    break
                data.extend(chunk[:remaining])
            ctype = r.headers.get('content-type', '')
            if 'gzip' in ctype.lower() or final.lower().endswith('.gz'):
                try:
                    raw = gzip.decompress(bytes(data))
                except Exception:
                    raw = bytes(data)
            else:
                raw = bytes(data)
            return FetchResult(url, final, r.status_code, raw.decode(r.encoding or 'utf-8', 'ignore'), ctype)
    except Exception as exc:
        return FetchResult(url, url, 0, '', '', type(exc).__name__)


def property_score(url: str) -> int:
    low = url.lower()
    score = 1
    for token, pts in (
        ('villa', 8), ('luxury', 6), ('beach', 5), ('ocean', 5), ('penthouse', 5), ('chalet', 4),
        ('property', 3), ('rental', 3), ('apartment', 2), ('cabin', 2), ('home', 1), ('stay', 1)
    ):
        if token in low:
            score += pts
    return min(100, score)


def slug_name(url: str) -> str:
    path = urlparse(url).path.rstrip('/').split('/')[-1]
    path = re.sub(r'[-_]+', ' ', path)
    path = re.sub(r'\s+', ' ', path).strip()
    return path[:160].title() if path else ''


def extract_public_links(base_url: str, text: str, root_domain: str) -> dict[str, Any]:
    links = [html.unescape(x.strip()) for x in HREF_RE.findall(text or '')]
    out: dict[str, Any] = {'instagram': '', 'facebook': '', 'whatsapp': '', 'contact_page': '', 'portfolio_url': ''}
    for href in links:
        u = urljoin(base_url, href)
        low = u.lower()
        if not out['instagram'] and ('instagram.com/' in low or 'instagr.am/' in low):
            out['instagram'] = u
        if not out['facebook'] and ('facebook.com/' in low or 'fb.com/' in low):
            out['facebook'] = u
        if not out['whatsapp'] and ('wa.me/' in low or 'api.whatsapp.com/' in low):
            out['whatsapp'] = u
        if same_site(u, root_domain):
            path = (urlparse(u).path or '').lower()
            if not out['contact_page'] and any(k in path for k in ('contact', 'reservations', 'enquiry', 'inquiry', 'book')):
                out['contact_page'] = u
            if not out['portfolio_url'] and any(k in path for k in ('properties', 'villas', 'rentals', 'accommodation', 'stays')):
                out['portfolio_url'] = u
    emails = sorted({m.group(1) for m in EMAIL_RE.finditer(text or '') if not m.group(1).lower().endswith(('.png','.jpg','.jpeg','.gif','.webp'))})
    out['public_email'] = emails[0] if emails else ''
    return out


def visible_excerpt(text: str, limit: int = 3200) -> str:
    x = re.sub(r'(?is)<script[^>]*>.*?</script>|<style[^>]*>.*?</style>', ' ', text or '')
    x = re.sub(r'(?s)<[^>]+>', ' ', x)
    x = html.unescape(x)
    x = re.sub(r'\s+', ' ', x).strip()
    return x[:limit]


def detect_pms(text: str) -> list[str]:
    low = (text or '').lower()
    return sorted(name for name, needles in PMS_PATTERNS.items() if any(n.lower() in low for n in needles))


def parse_sitemap(text: str) -> tuple[list[str], list[str]]:
    urls: list[str] = []
    child_maps: list[str] = []
    try:
        root = ET.fromstring(text.encode('utf-8', 'ignore'))
    except Exception:
        return urls, child_maps
    tag = root.tag.lower()
    locs = [str(el.text or '').strip() for el in root.iter() if el.tag.lower().endswith('loc') and str(el.text or '').strip()]
    if tag.endswith('sitemapindex'):
        child_maps.extend(locs)
    else:
        urls.extend(locs)
    return urls, child_maps


def expand_account(account: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    website = str(account.get('website') or '').strip()
    if not website:
        return {'status': 'NO_WEBSITE', 'assets': [], 'pms_fingerprints': [], 'first_party': {}, 'fetch_errors': ['NO_WEBSITE']}
    if '://' not in website:
        website = 'https://' + website
    host = urlparse(website).hostname or ''
    root_domain = registrable_domain(host)
    if not root_domain or not public_host(host):
        return {'status': 'UNSAFE_OR_UNRESOLVABLE_WEBSITE', 'assets': [], 'pms_fingerprints': [], 'first_party': {}, 'fetch_errors': ['UNSAFE_OR_UNRESOLVABLE_WEBSITE']}

    timeout = float(config.get('timeout_seconds') or 8)
    max_bytes = int(config.get('max_bytes_per_fetch') or 750000)
    max_sitemaps = int(config.get('max_sitemaps') or 5)
    max_urls = int(config.get('max_sitemap_urls') or 1500)
    max_assets = int(config.get('max_assets') or 250)
    sample_count = int(config.get('sample_count') or 5)
    session = requests.Session()
    session.headers.update({'User-Agent': 'AIProd-Hospitality-PortfolioResolver/2.0'})

    homepage = bounded_get(session, website, root_domain, timeout, max_bytes)
    page_text = homepage.text if homepage.status and homepage.status < 500 else ''
    first_party = extract_public_links(homepage.final_url or website, page_text, root_domain)
    pms = set(detect_pms(page_text))
    errors = [homepage.error] if homepage.error else []

    sitemap_candidates: list[str] = []
    robots_url = urljoin(homepage.final_url or website, '/robots.txt')
    robots = bounded_get(session, robots_url, root_domain, timeout, min(max_bytes, 250000))
    if robots.text:
        sitemap_candidates.extend(SITEMAP_RE.findall(robots.text))
    sitemap_candidates.extend([urljoin(homepage.final_url or website, '/sitemap.xml'), urljoin(homepage.final_url or website, '/sitemap_index.xml')])

    seen_maps: set[str] = set()
    all_urls: list[str] = []
    queue = list(dict.fromkeys(sitemap_candidates))
    while queue and len(seen_maps) < max_sitemaps and len(all_urls) < max_urls:
        sm = queue.pop(0)
        if sm in seen_maps or not same_site(sm, root_domain):
            continue
        seen_maps.add(sm)
        fr = bounded_get(session, sm, root_domain, timeout, max_bytes)
        if fr.error:
            errors.append(fr.error)
            continue
        urls, children = parse_sitemap(fr.text)
        all_urls.extend(u for u in urls if same_site(u, root_domain))
        for child in children:
            if same_site(child, root_domain) and child not in seen_maps:
                queue.append(child)

    property_urls: list[str] = []
    for u in dict.fromkeys(all_urls):
        low = u.lower()
        if any(x in low for x in UTILITY_HINTS):
            continue
        if any(x in low for x in PROPERTY_HINTS):
            property_urls.append(u)
        if len(property_urls) >= max_assets:
            break

    if first_party.get('portfolio_url') and same_site(first_party['portfolio_url'], root_domain):
        property_urls.append(first_party['portfolio_url'])
    property_urls = list(dict.fromkeys(property_urls))[:max_assets]
    assets = [
        {
            'url': u,
            'property_name': slug_name(u),
            'source_type': 'sitemap_or_first_party',
            'sample_priority': property_score(u),
        }
        for u in property_urls
    ]
    assets.sort(key=lambda x: (-int(x['sample_priority']), x['url']))
    samples = [x['url'] for x in assets[:sample_count]]
    portfolio_hash = __import__('hashlib').sha256('\n'.join(sorted(x['url'] for x in assets)).encode()).hexdigest() if assets else ''
    if not first_party.get('portfolio_url') and assets:
        first_party['portfolio_url'] = assets[0]['url']
    return {
        'status': 'OK' if homepage.status else 'PARTIAL',
        'homepage_status': homepage.status,
        'homepage_final_url': homepage.final_url,
        'homepage_excerpt': visible_excerpt(page_text),
        'assets': assets,
        'property_count_known': len(assets),
        'sample_property_urls': samples,
        'portfolio_hash': portfolio_hash,
        'pms_fingerprints': sorted(pms),
        'first_party': first_party,
        'fetch_errors': sorted(set(x for x in errors if x)),
    }
