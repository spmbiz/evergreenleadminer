#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import shutil
from pathlib import Path


def decode_file(path: Path):
    compact = "".join(path.read_text(encoding="utf-8").split())
    try:
        raw = gzip.decompress(base64.b64decode(compact, validate=True))
    except Exception as exc:
        raise RuntimeError(f"SHARD_DECODE_FAILED:{path.name}:{type(exc).__name__}:{exc}") from exc
    rows=[]
    for line_no,line in enumerate(raw.decode("utf-8").splitlines(),1):
        if not line.strip():
            continue
        try:
            row=json.loads(line)
        except Exception as exc:
            raise RuntimeError(f"SHARD_JSON_FAILED:{path.name}:{line_no}:{exc}") from exc
        if "r" not in row:
            raise RuntimeError(f"SHARD_ROW_MISSING_R:{path.name}:{line_no}")
        rows.append(row)
    return rows, hashlib.sha256(raw).hexdigest()


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--source-dir",required=True)
    ap.add_argument("--manifest",required=True)
    ap.add_argument("--outdir",required=True)
    ap.add_argument("--expected",type=int,default=5047)
    a=ap.parse_args()

    source=Path(a.source_dir)
    manifest_path=Path(a.manifest)
    out=Path(a.outdir)
    manifest=json.loads(manifest_path.read_text(encoding="utf-8"))
    shard_spec=manifest.get("shards") or {}
    expected_files=sorted(shard_spec)
    if not expected_files:
        raise RuntimeError("MANIFEST_HAS_NO_SHARDS")
    if int(manifest.get("expected_records",-1)) != a.expected:
        raise RuntimeError("MANIFEST_EXPECTED_MISMATCH")
    if int(manifest.get("unique_source_rows",-1)) != a.expected:
        raise RuntimeError("MANIFEST_UNIQUE_MISMATCH")

    actual_numeric=sorted(p.name for p in source.glob("queue_[0-9][0-9].jsonl.gz.b64"))
    if actual_numeric != expected_files:
        raise RuntimeError(f"SHARD_SET_MISMATCH expected={expected_files} actual={actual_numeric}")

    out.mkdir(parents=True,exist_ok=True)
    for old in out.glob("queue_*.jsonl.gz.b64"):
        old.unlink()

    by_r={}
    summaries=[]
    blank_names=0
    for name in expected_files:
        src=source/name
        rows,decoded_sha=decode_file(src)
        expected_count=int(shard_spec[name])
        if len(rows) != expected_count:
            raise RuntimeError(f"SHARD_COUNT_MISMATCH:{name}:expected={expected_count}:got={len(rows)}")
        for row in rows:
            r=int(row["r"])
            if r in by_r:
                if json.dumps(by_r[r],sort_keys=True,ensure_ascii=False) != json.dumps(row,sort_keys=True,ensure_ascii=False):
                    raise RuntimeError(f"CONFLICTING_DUPLICATE_SOURCE_ROW:{r}")
                raise RuntimeError(f"DUPLICATE_SOURCE_ROW:{r}")
            by_r[r]=row
            if not str(row.get("n") or "").strip():
                blank_names += 1
        shutil.copyfile(src,out/name)
        summaries.append({"file":name,"records":len(rows),"decoded_sha256":decoded_sha})

    if len(by_r) != a.expected:
        raise RuntimeError(f"TOTAL_UNIQUE_MISMATCH:expected={a.expected}:got={len(by_r)}")
    if blank_names != int(manifest.get("blank_business_names",blank_names)):
        raise RuntimeError(f"BLANK_NAME_COUNT_MISMATCH:manifest={manifest.get('blank_business_names')}:got={blank_names}")

    semantic=hashlib.sha256()
    for r in sorted(by_r):
        semantic.update((json.dumps(by_r[r],sort_keys=True,ensure_ascii=False,separators=(",",":"))+"\n").encode())
    summary={
        "schema":"gws-legacy-shard-guard-v1",
        "expected":a.expected,
        "loaded_unique":len(by_r),
        "blank_business_names":blank_names,
        "shards":summaries,
        "semantic_sha256":semantic.hexdigest(),
        "ok":True,
    }
    (out/"guard_summary.json").write_text(json.dumps(summary,indent=2)+"\n",encoding="utf-8")
    print("GWS_SHARD_GUARD="+json.dumps(summary,separators=(",",":")))


if __name__=="__main__":
    main()
