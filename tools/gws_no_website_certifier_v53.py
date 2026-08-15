#!/usr/bin/env python3
"""GWS v5.3 entrypoint with fail-closed Overture release fallback.

Core verifier logic is kept in gws_no_website_certifier_v53_core. The official
STAC catalog remains preferred. Only STAC unavailability may activate the
fallback release, and that release still has to pass the real S3/schema smoke
gate before any verification worker can run.
"""
from __future__ import annotations

import os
import sys

import gws_no_website_certifier_v53_core as _core

FALLBACK_RELEASE = os.getenv("OVERTURE_FALLBACK_RELEASE", "2026-06-17.0").strip()
_original_resolve = _core.resolve_overture_release


def resolve_overture_release() -> str:
    try:
        return _original_resolve()
    except RuntimeError as exc:
        # Do not hide invalid explicit pins or programming/configuration errors.
        if not str(exc).startswith("OVERTURE_STAC_UNAVAILABLE:"):
            raise
        if not _core.RELEASE_RE.match(FALLBACK_RELEASE):
            raise RuntimeError(f"INVALID_OVERTURE_FALLBACK_RELEASE:{FALLBACK_RELEASE}") from exc
        print(
            f"OVERTURE_STAC_DOWN_USING_SMOKE_VALIDATED_FALLBACK={FALLBACK_RELEASE}",
            file=sys.stderr,
        )
        return FALLBACK_RELEASE


_core.resolve_overture_release = resolve_overture_release

# Re-export the full implementation surface so deterministic tests keep testing
# the production entrypoint, including underscored parser/DNS helpers.
for _name, _value in vars(_core).items():
    if not _name.startswith("__"):
        globals()[_name] = _value
globals()["resolve_overture_release"] = resolve_overture_release


if __name__ == "__main__":
    _core.main()
