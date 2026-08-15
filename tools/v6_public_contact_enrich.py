#!/usr/bin/env python3
"""Bounded first-party public contact enrichment for hospitality recovery candidates.

Safety / integrity invariants:
- public HTTP(S) pages only; no login, forms, authentication, CAPTCHA bypass or JS automation;
- reject private/link-local/loopback/reserved network destinations before every redirect hop;
- stay on the candidate's registrable website domain;
- fetch at most a few pages per domain;
- only persist emails/social links that are explicitly published by the first-party site;
- never infer email patterns.

Output is compatible with the existing V6 live verifier and canonical aggregate.
"""
from __future__ import annotations

import argparse
import csv
import html as htmlmod
import ipaddress
import json
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

UA = "Mozilla/5.0 (compatible; AIProdLeadRecovery/1.0; public-business-research)"
MULTIPART_SUFFIXES = (
    "co.uk","org.uk","me.uk","ltd.uk","plc.uk","net.uk","com.au","net.au","org.au",
    "com.br","com.mx","co.nz","net.nz","org.nz","co.za","com.pt","com.es","com.tr",
    "co.jp","com.sg","com.hk","com.my"
)
BAD_EMAIL_DOMAINS = {
    "example.com","example.org","example.net","sentry.io","cloudflare.com","wixpress.com",
    "squarespace.com","wordpress.com","mailchimp.com","hubspot.com","booking.com","expedia.com",
    "tripadvisor.com","airbnb.com","facebook.com","instagram.com"
}
FREE_EMAIL = {
    "gmail.com","googlemail.com","outlook.com","hotmail.com","live.com","yahoo.com","icloud.com",
    "me.com","aol.com","proton.me","protonmail.com"
}
BAD_IG_PREFIXES = ("p/","reel/","reels/","stories/","explore/","accounts/","direct/","about/","legal/","developer/")
CONTACT_HINTS = (
    "contact","contact-us","contactus","about","about-us","aboutus","reservations","reservation",
    "booking","book-now","enquiries","enquiry","inquiries","inquiry","get-in-touch","reach-us"
)
ROLE_PREFIX_SCORE = {
    "reservations":100,"reservation":98,"booking":96,"bookings":95,"contact":94,"info":92,
    "hello":90,"stay":88,"office":86,"sales":84,"enquiries":82,"inquiries":82,"rentals":80,
    "management":78,"frontdesk":76,"reception":74,"host":60
}
_tls = threading.local()


def norm(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def host(url: str) -> str:
    try:
        h = (urlparse(url).hostname or "").lower().strip(".")
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""


def root_host(value: str) -> str:
    h = (value or "").lower().strip(".")
    if h.startswith("www."):
        h = h[4:]
    if not h:
        return ""
    for suffix in MULTIPART_SUFFIXES:
        if h == suffix:
            return h
        if h.endswith("." + suffix):
            parts = h.split(".")
            return ".".join(parts[-3:])
    parts = h.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else h


def email_domain(email: str) -> str:
    e = norm(email).lower().strip("<>[](){}.,;:\"'")
    return e.rsplit("@", 1)[1] if "@" in e else ""


def valid_email(email: str) -> bool:
    e = norm(email).lower().strip("<>[](){}.,;:\"'")
    if len(e) > 254 or not re.fullmatch(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", e):
        return False
    d = email_domain(e)
    if d in BAD_EMAIL_DOMAINS or any(d.endswith("." + x) for x in BAD_EMAIL_DOMAINS):
        return False
    if any(x in e for x in ("example@","test@","noreply@","no-reply@","donotreply@","privacy@privacy")):
        return False
    local = e.split("@", 1)[0]
    if local.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg")):
        return False
    return True


def session() -> requests.Session:
    s = getattr(_tls, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.2"})
        _tls.session = s
    return s


def public_host_only(hostname: str) -> bool:
    if not hostname or hostname.lower() == "localhost":
        return False
    try:
        infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return False
    ips = {item[4][0] for item in infos if item and item[4]}
    if not ips:
        return False
    for raw in ips:
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            return False
        if not ip.is_global:
            return False
    return True


def safe_url(url: str, allowed_root: str) -> bool:
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https"):
            return False
        h = (p.hostname or "").lower().strip(".")
        if not h or root_host(h) != allowed_root:
            return False
        if p.username or p.password:
            return False
        return public_host_only(h)
    except Exception:
        return False


def safe_get(url: str, allowed_root: str, timeout: float, max_bytes: int, max_redirects: int = 4):
    current = url
    for _ in range(max_redirects + 1):
        if not safe_url(current, allowed_root):
            return None, "UNSAFE_URL"
        try:
            r = session().get(current, timeout=timeout, allow_redirects=False, stream=True)
        except requests.Timeout:
            return None, "TIMEOUT"
        except requests.RequestException:
            return None, "NETWORK_ERROR"
        if r.status_code in (301, 302, 303, 307, 308):
            nxt = r.headers.get("Location")
            if not nxt:
                return None, f"HTTP_{r.status_code}_NO_LOCATION"
            current = urljoin(current, nxt)
            continue
        if r.status_code >= 400:
            return None, f"HTTP_{r.status_code}"
        ctype = (r.headers.get("Content-Type") or "").lower()
        if "html" not in ctype and "xhtml" not in ctype and ctype:
            return None, "NON_HTML"
        chunks = []
        total = 0
        try:
            for chunk in r.iter_content(chunk_size=65536, decode_unicode=False):
                if not chunk:
                    continue
                remaining = max_bytes - total
                if remaining <= 0:
                    break
                chunks.append(chunk[:remaining])
                total += min(len(chunk), remaining)
        finally:
            r.close()
        enc = r.encoding or "utf-8"
        try:
            text = b"".join(chunks).decode(enc, errors="replace")
        except LookupError:
            text = b"".join(chunks).decode("utf-8", errors="replace")
        return {"url": current, "status": r.status_code, "text": text}, "OK"
    return None, "REDIRECT_LIMIT"


def extract_links(raw_html: str, base_url: str):
    for href in re.findall(r"href\s*=\s*[\"']([^\"']+)[\"']", raw_html or "", flags=re.I):
        href = htmlmod.unescape(href).strip()
        if not href:
            continue
        yield href, urljoin(base_url, href)


def extract_emails(raw_html: str):
    text = htmlmod.unescape(raw_html or "")
    found = set()
    for href in re.findall(r"href\s*=\s*[\"']mailto:([^\"'?#]+)", text, flags=re.I):
        for part in re.split(r"[,;]", href):
            e = norm(part).lower().strip("<>[](){}.,;:\"'")
            if valid_email(e):
                found.add(e)
    scrubbed = re.sub(r"<script\b[^>]*>.*?</script>", " ", text, flags=re.I | re.S)
    scrubbed = re.sub(r"<style\b[^>]*>.*?</style>", " ", scrubbed, flags=re.I | re.S)
    for e in re.findall(r"(?i)(?<![A-Z0-9._%+\-])([A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,})(?![A-Z0-9._%+\-])", scrubbed):
        e = e.lower().strip("<>[](){}.,;:\"'")
        if valid_email(e):
            found.add(e)
    return found


def email_rank(email: str, website_root: str):
    e = email.lower()
    local, dom = e.split("@", 1)
    rd = root_host(dom)
    domain_class = 2 if rd == website_root else 1 if rd in FREE_EMAIL else 0
    prefix = re.split(r"[._+\-]", local)[0]
    role = ROLE_PREFIX_SCORE.get(prefix, 40)
    return domain_class, role, -len(e), e


def social_profiles(raw_html: str, base_url: str):
    ig = ""
    fb = ""
    for _href, url in extract_links(raw_html, base_url):
        try:
            p = urlparse(url)
            h = (p.hostname or "").lower().strip(".")
            if h.startswith("www."):
                h = h[4:]
            path = (p.path or "").strip("/")
            if h == "instagram.com" and not ig:
                if path and not any(path.lower().startswith(x) for x in BAD_IG_PREFIXES):
                    handle = path.split("/", 1)[0].strip()
                    if re.fullmatch(r"[A-Za-z0-9._]{1,30}", handle):
                        ig = f"https://www.instagram.com/{handle}/"
            elif h in ("facebook.com", "m.facebook.com") and not fb:
                if path and not path.lower().startswith(("sharer", "share", "dialog", "plugins", "login")):
                    fb = f"https://www.facebook.com/{path.split('?',1)[0].strip('/')}/"
        except Exception:
            continue
    return ig, fb


def candidate_contact_pages(raw_html: str, base_url: str, allowed_root: str):
    scored = []
    seen = set()
    for href, url in extract_links(raw_html, base_url):
        try:
            p = urlparse(url)
            if root_host((p.hostname or "").lower()) != allowed_root:
                continue
            path = (p.path or "").lower()
            text = href.lower()
            score = sum(1 for hint in CONTACT_HINTS if hint in path or hint in text)
            if score <= 0:
                continue
            clean = p._replace(fragment="").geturl()
            if clean not in seen:
                seen.add(clean)
                scored.append((score, clean))
        except Exception:
            continue
    scored.sort(key=lambda x: (-x[0], len(x[1]), x[1]))
    return [x[1] for x in scored]


def enrich(row: dict, timeout: float, max_pages: int, max_bytes: int):
    out = dict(row)
    website = norm(row.get("website"))
    allowed_root = root_host(host(website))
    out.update({
        "public_email":"", "email_domain":"", "email_domain_match":"",
        "instagram":"", "instagram_source_url":"", "facebook":"", "facebook_source_url":"",
        "contact_page":"", "email_source_url":"", "contact_recovery_status":"WITHHELD",
        "contact_recovery_reason":"", "pages_fetched":"0"
    })
    if not website or not allowed_root:
        out["contact_recovery_reason"] = "INVALID_WEBSITE"
        return out

    pages = []
    home, reason = safe_get(website, allowed_root, timeout, max_bytes)
    if not home:
        out["contact_recovery_reason"] = reason
        return out
    pages.append(home)

    candidates = candidate_contact_pages(home["text"], home["url"], allowed_root)
    if len(pages) < max_pages:
        fallback = [urljoin(home["url"], x) for x in ("/contact", "/contact-us", "/about", "/reservations")]
        for url in candidates + fallback:
            if len(pages) >= max_pages:
                break
            if any(p["url"] == url for p in pages):
                continue
            page, _ = safe_get(url, allowed_root, timeout, max_bytes)
            if page:
                pages.append(page)

    all_emails = set()
    email_sources = {}
    for page in pages:
        emails = extract_emails(page["text"])
        for email in emails:
            rd = root_host(email_domain(email))
            if rd != allowed_root and rd not in FREE_EMAIL:
                continue
            all_emails.add(email)
            email_sources.setdefault(email, page["url"])
        ig, fb = social_profiles(page["text"], page["url"])
        if ig and not out["instagram"]:
            out["instagram"] = ig
            out["instagram_source_url"] = page["url"]
        if fb and not out["facebook"]:
            out["facebook"] = fb
            out["facebook_source_url"] = page["url"]

    out["pages_fetched"] = str(len(pages))
    contact_like = [p["url"] for p in pages[1:]]
    if contact_like:
        out["contact_page"] = contact_like[0]
    if not all_emails:
        out["contact_recovery_reason"] = "NO_PUBLIC_FIRST_PARTY_EMAIL"
        return out

    best = max(all_emails, key=lambda e: email_rank(e, allowed_root))
    ed = root_host(email_domain(best))
    out["public_email"] = best
    out["email_domain"] = email_domain(best)
    out["email_domain_match"] = "YES" if ed == allowed_root else "FREE_WEBMAIL"
    out["email_source_url"] = email_sources.get(best, "")
    out["contact_recovery_status"] = "RECOVERED"
    out["contact_recovery_reason"] = "PUBLIC_FIRST_PARTY_EMAIL"
    out["notes"] = (norm(out.get("notes")) + " Public business email recovered from first-party website; no inference.").strip()
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--workers", type=int, default=48)
    ap.add_argument("--timeout", type=float, default=7.0)
    ap.add_argument("--max-pages", type=int, default=3)
    ap.add_argument("--max-bytes", type=int, default=900000)
    a = ap.parse_args()

    t0 = time.time()
    outdir = Path(a.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    with open(a.input, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))

    enriched = []
    with ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
        futures = [ex.submit(enrich, row, a.timeout, a.max_pages, a.max_bytes) for row in rows]
        for fut in as_completed(futures):
            enriched.append(fut.result())

    enriched.sort(key=lambda r: (r.get("contact_recovery_status") != "RECOVERED", r.get("fit_tier") != "A", -(int(r.get("operator_score") or 0)), -(int(r.get("premium_score") or 0)), r.get("name", "").lower()))
    fields = list(enriched[0].keys()) if enriched else []
    with (outdir / "v6_recovery_enriched.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(enriched)

    ready = [r for r in enriched if r.get("contact_recovery_status") == "RECOVERED" and r.get("public_email")]
    with (outdir / "v6_fast_ready.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(ready)

    reasons = {}
    for r in enriched:
        reason = r.get("contact_recovery_reason") or ""
        reasons[reason] = reasons.get(reason, 0) + 1
    summary = {
        "input_candidates": len(rows),
        "fast_ready": len(ready),
        "recovered_public_emails": len(ready),
        "instagram_found": sum(bool(r.get("instagram")) for r in enriched),
        "facebook_found": sum(bool(r.get("facebook")) for r in enriched),
        "contact_pages_found": sum(bool(r.get("contact_page")) for r in enriched),
        "pages_fetched": sum(int(r.get("pages_fetched") or 0) for r in enriched),
        "reasons": reasons,
        "elapsed_seconds": round(time.time() - t0, 2)
    }
    (outdir / "v6_contact_recovery_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    fast_summary_path = outdir / "v6_fast_summary.json"
    try:
        fast_summary = json.loads(fast_summary_path.read_text(encoding="utf-8"))
    except Exception:
        fast_summary = {}
    fast_summary.update({
        "fast_ready": len(ready),
        "recovery_candidates": len(rows),
        "recovered_public_emails": len(ready),
        "recovery_instagram_found": summary["instagram_found"],
        "recovery_facebook_found": summary["facebook_found"],
        "recovery_elapsed_seconds": summary["elapsed_seconds"]
    })
    fast_summary_path.write_text(json.dumps(fast_summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
