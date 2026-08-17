#!/usr/bin/env python3
"""Normalize self-hosted residential worker output into durable ingress evidence.

This is intentionally a separate contract from gws_residential_semantic_adapter:
worker raw -> residential_ingress -> semantic shadow input.
No result produced here can certify HIGH.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

SCHEMA = "gws-residential-ingress-v2"


def _providers(pass_ev: dict[str, Any]) -> list[str]:
    return [str(x) for x in (pass_ev.get("healthy_providers") or []) if str(x)]


def _direct(pass_ev: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for d in pass_ev.get("direct_health") or []:
        ident = d.get("identity") or {}
        out.append({
            "url": str(d.get("final") or d.get("seed") or ""),
            "ok": bool(d.get("ok")),
            "matched": bool(d.get("matched")),
            "name_overlap": ident.get("name_overlap"),
            "address_overlap": ident.get("address_overlap"),
            "postcode_hit": ident.get("postcode"),
            "phone_hit": ident.get("phone"),
            "error": str(d.get("error") or ""),
            "ownership_assessment": d.get("ownership_assessment") or {},
            "identity_match_withheld": d.get("identity_match_withheld") or {},
        })
    return out


def _queries(pass_ev: dict[str, Any], pass_no: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for q in pass_ev.get("search_health") or []:
        providers = q.get("providers") or []
        engines = []
        errors = []
        for p in providers:
            fam = str(p.get("provider_family") or p.get("provider") or "")
            if p.get("parsed") and fam:
                engines.append(fam)
            if p.get("error"):
                errors.append({"engine": fam, "error": str(p.get("error"))})
        out.append({
            "query": str(q.get("query") or ""),
            "ok": bool(q.get("raw_resultful")),
            "meta": {
                "engines_responded": sorted(set(engines)),
                "engine_errors": errors,
                "external_domains": int(q.get("external_domains") or 0),
                "residential_pass": pass_no,
            },
        })
    return out


def normalize_event(ev: dict[str, Any]) -> dict[str, Any] | None:
    # Already-normalized ingress stays compatible/idempotent.
    if ev.get("record_key") and not ev.get("candidate"):
        out = dict(ev)
        out.setdefault("schema", SCHEMA)
        out["final_high"] = False
        return out

    cand = ev.get("candidate") or {}
    key = str(cand.get("record_key") or ev.get("record_key") or "").strip()
    if not key:
        return None

    p1 = ev.get("pass1") or {}
    p2 = ev.get("pass2") or {}
    raw_status = str(ev.get("status") or "").upper()
    owned = str(ev.get("owned_site") or "").strip()
    if raw_status == "REJECT" and owned:
        status = "OWNED_SITE_CONFIRMED"
    elif raw_status in {"EVIDENCE_INCOMPLETE", "SEARCH_INCOMPLETE"}:
        status = "SEARCH_INCOMPLETE"
    else:
        status = raw_status or "UNCERTAIN"

    engines = sorted(set(_providers(p1) + _providers(p2)))
    return {
        "schema": SCHEMA,
        "record_key": key,
        "source_fingerprint": str(cand.get("fingerprint") or ev.get("source_fingerprint") or ""),
        "hub_name": str(cand.get("n") or cand.get("alias") or ev.get("hub_name") or ""),
        "hub_address": str(cand.get("a") or ev.get("hub_address") or ""),
        "hub_postalcode": str(cand.get("p") or ev.get("hub_postalcode") or ""),
        "observed_at": str(cand.get("observed_at") or ev.get("observed_at") or ""),
        "status": status,
        "worker_status": raw_status,
        "worker_reason": str(ev.get("reason") or ""),
        "owned_site": owned if status == "OWNED_SITE_CONFIRMED" else "",
        "engines_responded": engines,
        "queries": _queries(p1, 1) + _queries(p2, 2),
        "direct_evidence": _direct(p1) + _direct(p2),
        "certificate": ev.get("certificate") or {},
        "certificate_eligible": bool(ev.get("certificate_eligible")),
        "terminal_pass1_reject": bool(ev.get("terminal_pass1_reject")),
        "final_high": False,
        "note": "Residential OpenSERP evidence only; downstream strict verifier remains authoritative and no-site absence is not promoted here.",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--expect", type=int, default=-1)
    args = ap.parse_args()

    inp = Path(args.input)
    raw_lines = [x for x in inp.read_text(encoding="utf-8").splitlines() if x.strip()] if inp.exists() else []
    rows = []
    dropped = 0
    for line in raw_lines:
        row = normalize_event(json.loads(line))
        if row is None:
            dropped += 1
        else:
            rows.append(row)

    # Last observation per key inside this worker batch; duplicates are not useful.
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        latest[str(row["record_key"])] = row
    rows = list(latest.values())

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True, default=str) + "\n" for r in rows), encoding="utf-8")
    report = {
        "schema": SCHEMA,
        "raw_input": len(raw_lines),
        "output_unique": len(rows),
        "dropped_missing_key": dropped,
        "owned_confirmed": sum(1 for r in rows if r.get("status") == "OWNED_SITE_CONFIRMED"),
        "search_incomplete": sum(1 for r in rows if r.get("status") == "SEARCH_INCOMPLETE"),
    }
    print("GWS_RESIDENTIAL_INGRESS_ADAPTER=" + json.dumps(report, separators=(",", ":")))
    if args.expect >= 0 and len(rows) != args.expect:
        raise SystemExit(f"residential ingress count mismatch: expected={args.expect} actual={len(rows)} raw={len(raw_lines)} dropped={dropped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
