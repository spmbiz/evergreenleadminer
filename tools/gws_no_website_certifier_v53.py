#!/usr/bin/env python3
"""GWS v5.3 production entrypoint.

Core verifier logic is kept in gws_no_website_certifier_v53_core. This entrypoint
adds two runtime-hardening layers discovered by live GitHub calibration:
1) fail-closed Overture STAC fallback that still must pass S3/schema smoke;
2) a throttled multi-provider search pool resilient to GitHub-runner rate limits.
"""
from __future__ import annotations

import os
import sys

import gws_no_website_certifier_v53_core as _core
import gws_search_provider_pool_v54 as _providers

FALLBACK_RELEASE = os.getenv("OVERTURE_FALLBACK_RELEASE", "2026-06-17.0").strip()
_original_resolve = _core.resolve_overture_release


def resolve_overture_release() -> str:
    try:
        return _original_resolve()
    except RuntimeError as exc:
        # Never mask a bad explicit pin or another programming/configuration error.
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

# v5.run_web delegates to v4.webcheck, so switch the production runtime to the
# provider pool validated from a live GitHub Actions runner.
_core.webcheck_hardened = _providers.webcheck
_core.v4.webcheck = _providers.webcheck

# Re-export the implementation surface so deterministic tests test the same
# production entrypoint, including underscored parser/DNS helpers.
for _name, _value in vars(_core).items():
    if not _name.startswith("__"):
        globals()[_name] = _value
globals()["resolve_overture_release"] = resolve_overture_release


if __name__ == "__main__":
    _core.main()
