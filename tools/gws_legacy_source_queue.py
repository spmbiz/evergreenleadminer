#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import csv
import gzip
import hashlib
import io
import json
import re
import time
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

REQUIRED = {
    "business_name",
    "commune",
    "postal_code",
    "street_address",
    "phone",
    "source_url_clean",
    "google_maps_url",
    "confidence_clean",
    "dedup_key_v2",
    "Reconciliation Status",
    "Current Official Website",
}
BRUSSELS_POSTCODES = {
    "1000", "1020", "1030", "1040", "1050", "1060", "1070", "1080",
    "1081", "1082", "1083", "1090", "1120", "1130", "1140", "1150",
    "1160", "1170", "1180", "1190", "1200", "1210",
}
UA = "Mozilla/5.0 GWS-Legacy-Source-Snapshot/1.0"


def t(v):
    return re.sub(r"\s+", " ", str(v or "")).strip()


def download_csv(sheet_id: str, gid: str, timeout: int = 45) -> tuple[bytes, str, str]:
    url = (
        "https://docs.google.com/spreadsheets/d/"
        + urllib.parse.quote(sheet_id, safe="")
        + "/export?format=csv&gid="
        + urllib.parse.quote(gid, safe="")
    )
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/csv,text/plain;q=0.9,*/*;q=0.1",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        ctype = t(r.headers.get("Content-Type")).lower()
        final_url = r.geturl()
    head = body[:500].lower()
    if b"<html" in head or b"accounts.google.com" in body[:5000].lower():
        raise RuntimeError("GOOGLE_SOURCE_NOT_PUBLIC_OR_AUTH_REDIRECT")
    if len(body) < 1000:
        raise RuntimeError(f"GOOGLE_SOURCE_TOO_SMALL:{len(body)}")
    return body, ctype, final_url


def build(body: bytes, expected: int, source_url: str, outdir: Path):
    text = body.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    headers = set(reader.fieldnames or [])
    missing = sorted(REQUIRED - headers)
    if missing:
        raise RuntimeError("SOURCE_HEADERS_MISSING:" + ",".join(missing))

    rows = list(reader)
    if len(rows) != expected:
        raise RuntimeError(f"SOURCE_COUNT_MISMATCH expected={expected} got={len(rows)}")

    queue = []
    status_counts = Counter()
    postcode_counts = Counter()
    blank_name = blank_postcode = blank_dedup = 0
    for sheet_row, r in enumerate(rows, start=2):
        name = t(r.get("business_name"))
        pc = re.sub(r"\D", "", t(r.get("postal_code")))[:4]
        status = t(r.get("Reconciliation Status"))
        if not name:
            blank_name += 1
        if not pc:
            blank_postcode += 1
        if not t(r.get("dedup_key_v2")):
            blank_dedup += 1
        status_counts[status or "<BLANK>"] += 1
        postcode_counts[pc or "<BLANK>"] += 1
        queue.append(
            {
                "r": sheet_row,
                "sr": t(r.get("source_row")),
                "n": name,
                "cn": t(r.get("canonical_business_name")),
                "c": t(r.get("commune")),
                "p": pc,
                "a": t(r.get("street_address")),
                "ph": t(r.get("phone")),
                "em": t(r.get("email")),
                "su": t(r.get("source_url_clean")),
                "gm": t(r.get("google_maps_url")),
                "conf": t(r.get("confidence_clean")),
                "dk": t(r.get("dedup_key_v2")),
                "rec": status,
                "cow": t(r.get("Current Official Website")),
                "ow": t(r.get("official_website")),
                "vb": t(r.get("vertical_bucket")),
                "vo": t(r.get("vertical_original")),
            }
        )

    # Spreadsheet row identity must be one-to-one inside this source snapshot.
    ids = [x["r"] for x in queue]
    if len(ids) != len(set(ids)):
        raise RuntimeError("DUPLICATE_ACTIONABLE_SHEET_ROW")

    raw = ("\n".join(json.dumps(x, ensure_ascii=False, separators=(",", ":")) for x in queue) + "\n").encode("utf-8")
    gz = gzip.compress(raw, compresslevel=9)
    b64 = base64.b64encode(gz)

    outdir.mkdir(parents=True, exist_ok=True)
    queue_path = outdir / f"queue_{expected}.jsonl.gz.b64"
    queue_path.write_bytes(b64 + b"\n")
    (outdir / "source.csv.gz").write_bytes(gzip.compress(body, compresslevel=9))

    in_scope = sum(1 for x in queue if x["p"] in BRUSSELS_POSTCODES)
    manifest = {
        "schema_version": 1,
        "source": "GOOGLE_SHEETS_ACTIONABLE_NO_SITE",
        "source_url": source_url,
        "expected": expected,
        "loaded": len(queue),
        "sheet_row_first": ids[0] if ids else None,
        "sheet_row_last": ids[-1] if ids else None,
        "in_scope_brussels_postcode": in_scope,
        "outside_or_unknown_postcode": len(queue) - in_scope,
        "blank_business_name": blank_name,
        "blank_postcode": blank_postcode,
        "blank_dedup_key_v2": blank_dedup,
        "reconciliation_status_counts": dict(status_counts),
        "top_postcodes": dict(postcode_counts.most_common(30)),
        "csv_sha256": hashlib.sha256(body).hexdigest(),
        "jsonl_sha256": hashlib.sha256(raw).hexdigest(),
        "gzip_sha256": hashlib.sha256(gz).hexdigest(),
        "queue_b64_bytes": len(b64),
        "snapshot_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (outdir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return manifest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet-id", required=True)
    ap.add_argument("--gid", required=True)
    ap.add_argument("--expected", type=int, required=True)
    ap.add_argument("--outdir", required=True)
    a = ap.parse_args()
    body, ctype, final_url = download_csv(a.sheet_id, a.gid)
    manifest = build(body, a.expected, final_url, Path(a.outdir))
    manifest["content_type"] = ctype
    print("SOURCE_QUEUE=" + json.dumps(manifest, separators=(",", ":"), ensure_ascii=False))


if __name__ == "__main__":
    main()
