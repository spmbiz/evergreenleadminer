#!/usr/bin/env python3
"""Legacy deep verifier v3.

Adds public-email-domain candidates to the existing adversarial v2 verifier.
An email domain is only a URL candidate: v2 still requires HTTP success and
identity evidence before classifying it as an owned website.
"""
from __future__ import annotations

import re
import gws_legacy_deep_v2 as v2

_FREE_MAIL = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "hotmail.be",
    "live.com", "live.be", "msn.com", "yahoo.com", "yahoo.fr", "icloud.com",
    "me.com", "proton.me", "protonmail.com", "skynet.be", "telenet.be",
    "scarlet.be", "voo.be",
}
_ORIGINAL_GUESSES = v2.guesses


def _email_domains(candidate: dict) -> list[str]:
    value = v2.t(candidate.get("em"))
    domains: list[str] = []
    for email in re.split(r"[\s,;]+", value):
        if "@" not in email:
            continue
        domain = email.rsplit("@", 1)[1].lower().strip(" .<>()[]{}\"'")
        if not domain or "." not in domain or domain in _FREE_MAIL:
            continue
        if domain not in domains:
            domains.append(domain)
    return domains


def guesses(candidate: dict) -> list[str]:
    out: list[str] = []
    for domain in _email_domains(candidate):
        out.extend((f"https://{domain}/", f"https://www.{domain}/"))
    for url in _ORIGINAL_GUESSES(candidate):
        if url not in out:
            out.append(url)
    return out[:12]


v2.guesses = guesses

if __name__ == "__main__":
    v2.main()
