#!/usr/bin/env python3
"""Normalize home OpenSERP v3 observations into durable residential ingress v1.

The output is deliberately shadow/evidence-only. Absence of a discovered site is
never converted to HIGH here; downstream strict verification remains authoritative.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def parsed_engines(search_health):
    out = []
    for q in search_health or []:
        for p in q.get("providers") or []:
            if p.get("parsed") and p.get("provider") and p["provider"] not in out:
                out.append(p["provider"])
    return out


def query_rows(pass_data):
    rows = []
    for q in pass_data.get("search_health") or []:
        providers = q.get("providers") or []
        responded = [p.get("provider") for p in providers if p.get("parsed") and p.get("provider")]
        failed = [
            {"engine": p.get("provider"), "error": p.get("error") or ("blocked" if p.get("blocked") else "unparsed")}
            for p in providers if not p.get("parsed") and p.get("provider")
        ]
        rows.append({
            "query": q.get("query") or "",
            "ok": len(set(q.get("parsed_families") or [])) >= 2,
            "meta": {
                "engines_responded": responded,
                "engine_errors": failed,
                "external_domains": q.get("external_domains") or 0,
                "residential_pass": pass_data.get("residential_pass"),
            },
        })
    return rows


def direct_rows(pass_data):
    out = []
    for d in pass_data.get("direct_health") or []:
        ident = d.get("identity") or {}
        out.append({
            "ok": bool(d.get("ok")),
            "url": d.get("final") or d.get("seed") or "",
            "matched": bool(d.get("matched") or ident.get("matched")),
            "name_overlap": ident.get("page_name_overlap", ident.get("name_overlap")),
            "address_overlap": ident.get("address_overlap"),
            "postcode_hit": ident.get("postcode_match", ident.get("postcode_hit")),
            "phone_hit": ident.get("phone_exact", ident.get("phone_hit")),
            "error": d.get("error") or "",
        })
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    a = ap.parse_args()

    rows = [json.loads(line) for line in Path(a.input).read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    out = []
    counts = {}
    for x in rows:
        c = x.get("candidate") or {}
        pe = x.get("place") or {}
        p1 = x.get("pass1") or {}
        p2 = x.get("pass2") or {}
        key = str(c.get("record_key") or "").strip()
        if not key:
            oid = str(pe.get("overture_id") or "").strip()
            key = "overture:" + oid if oid else ""
        if not key:
            continue

        owned = str(x.get("owned_site") or p1.get("owned") or p2.get("owned") or "").strip()
        worker_status = str(x.get("status") or "").upper()
        if owned:
            status = "OWNED_SITE_CONFIRMED"
        elif worker_status == "EVIDENCE_COMPLETE":
            status = "NO_OWNED_SITE_OBSERVED"
        elif worker_status == "REJECT":
            status = "SEARCH_REJECT"
        else:
            status = "SEARCH_INCOMPLETE"
        counts[status] = counts.get(status, 0) + 1

        engines = []
        for e in list(p1.get("healthy_providers") or []) + list(p2.get("healthy_providers") or []) + parsed_engines(p1.get("search_health")) + parsed_engines(p2.get("search_health")):
            if e and e not in engines:
                engines.append(e)

        out.append({
            "record_key": key,
            "hub_name": c.get("n") or "",
            "hub_address": c.get("a") or "",
            "hub_postalcode": c.get("p") or "",
            "source_fingerprint": c.get("fingerprint") or "",
            "observed_at": c.get("observed_at") or "",
            "status": status,
            "owned_site": owned,
            "engines_responded": engines,
            "queries": query_rows(p1) + query_rows(p2),
            "direct_evidence": direct_rows(p1) + direct_rows(p2),
            "certificate": x.get("certificate") or {},
            "worker_reason": x.get("reason") or "",
            "final_high": False,
            "note": "Residential OpenSERP evidence only; downstream strict verifier remains authoritative and no-site absence is not promoted here.",
        })

    dest = Path(a.output)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("".join(json.dumps(x, ensure_ascii=False, sort_keys=True, default=str) + "\n" for x in out), encoding="utf-8")
    print("GWS_HOME_INGRESS_ADAPTER=" + json.dumps({"input": len(rows), "output": len(out), "statuses": counts}, separators=(",", ":")))


if __name__ == "__main__":
    main()
