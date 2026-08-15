#!/usr/bin/env python3
"""Probe the official AllThePlaces Parquet output with DuckDB HTTP range reads.

The probe never downloads the full dataset. It resolves the latest run metadata,
loads the remote Parquet footer/schema through DuckDB httpfs, asks for row count,
and samples a tiny bounded set of rows/columns. Results are persisted for deciding
whether Parquet should become the production ATP transport.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import duckdb
import requests

LATEST = "https://data.alltheplaces.xyz/runs/latest.json"


def qlit(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--sample", type=int, default=3)
    a = ap.parse_args()

    t0 = time.time()
    r = requests.get(LATEST, timeout=20, headers={"User-Agent":"AIProdLeadHarvester/1.0 (+public-data-research)"})
    r.raise_for_status()
    meta = r.json()
    url = str(meta.get("parquet_url") or "")
    if not url:
        raise RuntimeError("latest ATP metadata has no parquet_url")

    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; SET enable_progress_bar=false; SET threads=4;")
    probe = {
        "run_id": meta.get("run_id"),
        "parquet_url": url,
        "declared_total_lines": meta.get("total_lines"),
        "declared_spiders": meta.get("spiders"),
    }

    t_schema = time.time()
    desc = con.execute(f"DESCRIBE SELECT * FROM read_parquet({qlit(url)})").fetchall()
    probe["schema_seconds"] = round(time.time() - t_schema, 3)
    probe["schema"] = [
        {"column_name": row[0], "column_type": row[1], "null": row[2], "key": row[3], "default": row[4], "extra": row[5]}
        for row in desc
    ]

    t_count = time.time()
    count = con.execute(f"SELECT count(*) FROM read_parquet({qlit(url)})").fetchone()[0]
    probe["count"] = int(count)
    probe["count_seconds"] = round(time.time() - t_count, 3)

    names = [x["column_name"] for x in probe["schema"]]
    preferred = [
        "name","brand","operator","website","email","phone","country","city","state","street",
        "category","categories","tourism","amenity","addr:country","@spider","@source_uri","id","ref"
    ]
    picked = [x for x in preferred if x in names]
    if not picked:
        picked = names[: min(12, len(names))]
    probe["sample_columns"] = picked

    cols = ",".join('"'+c.replace('"','""')+'"' for c in picked)
    t_sample = time.time()
    rows = con.execute(f"SELECT {cols} FROM read_parquet({qlit(url)}) LIMIT {max(1, int(a.sample))}").fetchall()
    probe["sample_seconds"] = round(time.time() - t_sample, 3)
    probe["sample"] = [dict(zip(picked, row)) for row in rows]
    probe["elapsed_seconds"] = round(time.time() - t0, 3)
    probe["transport_ok"] = True

    p = Path(a.out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(probe, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps(probe, indent=2, default=str))


if __name__ == "__main__":
    main()
