#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import bz2
import csv
import gzip
import hashlib
import io
import json
import re
from pathlib import Path


def norm(s):
    return re.sub(r"[^a-z0-9]+", "_", str(s or "").strip().lower()).strip("_")


def compact_text(p: Path) -> str:
    return "".join(p.read_text(encoding="utf-8").split())


def pick(d, *keys):
    nd = {norm(k): v for k, v in d.items()}
    for k in keys:
        if norm(k) in nd and str(nd[norm(k)] or "").strip():
            return nd[norm(k)]
    return ""


def canonicalize(rows):
    out = []
    for i, d in enumerate(rows, 1):
        if not isinstance(d, dict):
            continue
        r = pick(d, "r", "source_row", "sheet_row", "row", "row_number")
        n = pick(d, "n", "business_name", "name", "company_name", "company")
        p = pick(d, "p", "postal_code", "postcode", "zip", "zip_code")
        a = pick(d, "a", "street_address", "address", "street")
        ph = pick(d, "ph", "phone", "telephone", "tel")
        em = pick(d, "em", "email", "emails")
        cow = pick(d, "cow", "current_official_website", "official_website", "current_website")
        if r == "":
            raise RuntimeError(f"SOURCE_ROW_MISSING at decoded row {i}")
        try:
            rr = int(float(str(r).strip()))
        except Exception as e:
            raise RuntimeError(f"SOURCE_ROW_INVALID:{r!r}") from e
        if not str(n or "").strip():
            raise RuntimeError(f"BUSINESS_NAME_MISSING source_row={rr}")
        out.append({"r": rr, "n": str(n).strip(), "p": str(p or "").strip(), "a": str(a or "").strip(), "ph": str(ph or "").strip(), "em": str(em or "").strip(), "cow": str(cow or "").strip()})
    return out


def parse_text(raw: bytes):
    text = raw.decode("utf-8-sig")
    s = text.lstrip()
    if s.startswith("["):
        obj = json.loads(text)
        if isinstance(obj, list):
            return obj
    if s.startswith("{"):
        try:
            obj = json.loads(text)
            if isinstance(obj, list):
                return obj
            if isinstance(obj, dict):
                for k in ("rows", "records", "data", "items"):
                    if isinstance(obj.get(k), list):
                        return obj[k]
        except json.JSONDecodeError:
            pass
    lines = [x for x in text.splitlines() if x.strip()]
    if lines:
        try:
            vals = [json.loads(x) for x in lines]
            if vals and all(isinstance(x, dict) for x in vals):
                return vals
        except Exception:
            pass
    sample = text[:10000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        rdr = csv.DictReader(io.StringIO(text), dialect=dialect)
        vals = list(rdr)
        if vals:
            return vals
    except Exception:
        pass
    raise RuntimeError("DECODED_SNAPSHOT_FORMAT_UNSUPPORTED")


def decode_snapshot(input_dir: Path):
    parts = sorted(input_dir.glob("part_*.b64"))
    if not parts:
        raise RuntimeError(f"NO_SNAPSHOT_PARTS:{input_dir}")
    chunks = [(p, compact_text(p)) for p in parts]

    # Some earlier uploads left a shorter duplicate-prefix fragment (e.g. part_02a)
    # beside the complete chunk. Exclude only when its entire payload is a prefix
    # of another retained payload; this is deterministic and content-based.
    retained = []
    ignored = []
    for p, s in chunks:
        duplicate_prefix = any(p != q and len(s) < len(t) and t.startswith(s) for q, t in chunks)
        if duplicate_prefix:
            ignored.append(p.name)
        else:
            retained.append((p, s))
    retained.sort(key=lambda x: x[0].name)
    joined = "".join(s for _, s in retained)
    joined += "=" * ((4 - len(joined) % 4) % 4)
    try:
        packed = base64.b64decode(joined, validate=True)
    except Exception as e:
        raise RuntimeError(f"SNAPSHOT_BASE64_INVALID:{type(e).__name__}:{e}") from e

    decoders = [("bz2", bz2.decompress), ("gzip", gzip.decompress)]
    raw = None
    codec = None
    for name, fn in decoders:
        try:
            raw = fn(packed)
            codec = name
            break
        except Exception:
            pass
    if raw is None:
        raw = packed
        codec = "raw"
    rows = parse_text(raw)
    return rows, {
        "parts": [{"file": p.name, "sha256": hashlib.sha256(p.read_bytes()).hexdigest(), "chars": len(s)} for p, s in retained],
        "ignored_duplicate_prefix_parts": ignored,
        "packed_sha256": hashlib.sha256(packed).hexdigest(),
        "decoded_sha256": hashlib.sha256(raw).hexdigest(),
        "codec": codec,
    }


def write_queue(path: Path, rows):
    raw = ("\n".join(json.dumps(x, ensure_ascii=False, separators=(",", ":")) for x in rows) + "\n").encode()
    enc = base64.b64encode(gzip.compress(raw, 9)).decode()
    path.write_text(enc + "\n", encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input-dir", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--expected", type=int, required=True)
    ap.add_argument("--shards", type=int, default=12)
    a = ap.parse_args()

    source_rows, source_meta = decode_snapshot(Path(a.input_dir))
    rows = canonicalize(source_rows)
    by = {}
    for row in rows:
        r = row["r"]
        if r in by and by[r] != row:
            raise RuntimeError(f"CONFLICTING_DUPLICATE_SOURCE_ROW:{r}")
        by[r] = row
    rows = [by[k] for k in sorted(by)]
    if len(rows) != a.expected:
        raise SystemExit(f"SNAPSHOT_COUNT_MISMATCH expected={a.expected} got={len(rows)}")

    d = Path(a.outdir)
    d.mkdir(parents=True, exist_ok=True)
    for old in d.glob("queue_*.jsonl.gz.b64"):
        old.unlink()
    shard_meta = []
    for i in range(a.shards):
        shard = rows[i::a.shards]
        p = d / f"queue_{i:02d}.jsonl.gz.b64"
        sha = write_queue(p, shard)
        shard_meta.append({"file": p.name, "records": len(shard), "sha256": sha})

    manifest = {
        "schema": "gws-legacy-immutable-snapshot-v1",
        "expected_records": a.expected,
        "unique_records": len(rows),
        "source": source_meta,
        "shards": shard_meta,
    }
    (d / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("SNAPSHOT_MATERIALIZED=" + json.dumps({"expected": a.expected, "unique": len(rows), "codec": source_meta["codec"], "parts": [x["file"] for x in source_meta["parts"]], "ignored": source_meta["ignored_duplicate_prefix_parts"], "shards": len(shard_meta)}, separators=(",", ":")))


if __name__ == "__main__":
    main()
