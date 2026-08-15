#!/usr/bin/env python3
"""Compatibility wrapper for the hardened GWS v5.3 certifier.

The official Overture STAC endpoint is preferred. If (and only if) STAC itself
is unavailable, fall back to a known release pin; the certifier's Overture
smoke query must still open that S3 release and validate its schema before any
verification worker is allowed to run.
"""
from __future__ import annotations

import os
import sys

import gws_no_website_certifier_v53 as core

FALLBACK_RELEASE = os.getenv("OVERTURE_FALLBACK_RELEASE", "2026-06-17.0").strip()
_original_resolve = core.resolve_overture_release


def resolve_overture_release() -> str:
    try:
        return _original_resolve()
    except RuntimeError as exc:
        # Never mask a bad explicit pin or any other programming/config error.
        if not str(exc).startswith("OVERTURE_STAC_UNAVAILABLE:"):
            raise
        if not core.RELEASE_RE.match(FALLBACK_RELEASE):
            raise RuntimeError(f"INVALID_OVERTURE_FALLBACK_RELEASE:{FALLBACK_RELEASE}") from exc
        print(
            f"OVERTURE_STAC_DOWN_USING_SMOKE_VALIDATED_FALLBACK={FALLBACK_RELEASE}",
            file=sys.stderr,
        )
        return FALLBACK_RELEASE


# All functions in the core module resolve this global dynamically, so replacing
# it here hardens smoke + workers without duplicating the verifier implementation.
core.resolve_overture_release = resolve_overture_release

# Re-export the testable/public surface expected by gws_v5_selftest.py.
for _name, _value in vars(core).items():
    if not _name.startswith("__"):
        globals().setdefault(_name, _value)
globals()["resolve_overture_release"] = resolve_overture_release


if __name__ == "__main__":
    core.main()
