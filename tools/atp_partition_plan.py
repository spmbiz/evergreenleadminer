#!/usr/bin/env python3
"""Build deterministic size-balanced AllThePlaces ZIP partitions via HTTP Range.

Only ZIP metadata is read when planning. Members are assigned largest-first to the
currently lightest partition so one giant spider does not create a pathological
worker while other workers idle. The exact member list is persisted for audit and
resume; workers never need to guess the latest archive independently.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import zipfile
from pathlib import Path

import fsspec
import requests

INFO = "https://data.alltheplaces.xyz/runs/latest/info_embed.html"
ARCHIVE_RE = re.compile(r"href=[\"'](https://alltheplaces-data\.openaddresses\.io/runs/[^\"']+/output\.zip)", re.I)
DATA_SUFFIXES = (".geojson", ".json", ".geojson.gz", ".ndjson", ".ndjson.gz")
EXCLUDE_NAMES = ("summary", "stats", "metadata", "manifest", "index")


def now_z() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def latest_url() -> str:
    r = requests.get(INFO, timeout=20)
    r.raise_for_status()
    m = ARCHIVE_RE.search(r.text)
    if not m:
        raise RuntimeError("latest AllThePlaces output.zip URL not found")
    return m.group(1)


def is_data_member(name: str) -> bool:
    low = name.lower()
    if not low.endswith(DATA_SUFFIXES):
        return False
    base = Path(low).name
    return not any(x in base for x in EXCLUDE_NAMES)


def open_remote(url: str, block_mb: int):
    return fsspec.open(url, "rb", block_size=max(1, block_mb) * 1024 * 1024, cache_type="readahead").open()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--partitions", type=int, default=64)
    ap.add_argument("--archive-url", default="")
    ap.add_argument("--block-mb", type=int, default=4)
    a = ap.parse_args()

    url = a.archive_url or latest_url()
    n = max(1, int(a.partitions))
    with open_remote(url, a.block_mb) as remote:
        if not remote.seekable():
            raise RuntimeError("ATP HTTP archive is not seekable; Range transport unavailable")
        with zipfile.ZipFile(remote) as z:
            infos = [i for i in z.infolist() if not i.is_dir() and is_data_member(i.filename)]

    # Stable order before LPT bin packing. Equal-size members use filename tie-break.
    infos.sort(key=lambda i: (-int(i.compress_size), i.filename))
    bins = [{"index": i, "compressed_bytes": 0, "uncompressed_bytes": 0, "members": []} for i in range(n)]
    for info in infos:
        b = min(bins, key=lambda x: (x["compressed_bytes"], len(x["members"]), x["index"]))
        b["members"].append({
            "name": info.filename,
            "compressed_bytes": int(info.compress_size),
            "uncompressed_bytes": int(info.file_size),
            "crc32": f"{int(info.CRC):08x}",
        })
        b["compressed_bytes"] += int(info.compress_size)
        b["uncompressed_bytes"] += int(info.file_size)

    for b in bins:
        b["members"].sort(key=lambda x: x["name"])

    run_match = re.search(r"/runs/([^/]+)/output\.zip", url)
    payload = {
        "schema_version": 1,
        "source": "AllThePlaces",
        "archive_url": url,
        "archive_run": run_match.group(1) if run_match else "",
        "generated_at": now_z(),
        "partition_count": n,
        "member_count": len(infos),
        "compressed_bytes_total": sum(int(i.compress_size) for i in infos),
        "uncompressed_bytes_total": sum(int(i.file_size) for i in infos),
        "max_partition_compressed_bytes": max((b["compressed_bytes"] for b in bins), default=0),
        "min_partition_compressed_bytes": min((b["compressed_bytes"] for b in bins), default=0),
        "partitions": bins,
        "note": "Exact remote ZIP member manifest. Size-balanced largest-processing-time assignment; workers stream only assigned members via HTTP Range."
    }
    p = Path(a.out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in payload.items() if k != "partitions"}, indent=2))


if __name__ == "__main__":
    main()
