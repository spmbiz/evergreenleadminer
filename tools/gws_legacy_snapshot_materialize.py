#!/usr/bin/env python3
from __future__ import annotations

import argparse, base64, bz2, csv, gzip, hashlib, io, json, re
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
    out=[]
    for i,d in enumerate(rows,1):
        if not isinstance(d,dict): continue
        r=pick(d,"r","source_row","sheet_row","row","row_number")
        n=pick(d,"n","business_name","name","company_name","company")
        p=pick(d,"p","postal_code","postcode","zip","zip_code")
        a=pick(d,"a","street_address","address","street")
        ph=pick(d,"ph","phone","telephone","tel")
        em=pick(d,"em","email","emails")
        cow=pick(d,"cow","current_official_website","official_website","current_website")
        if r=="": raise RuntimeError(f"SOURCE_ROW_MISSING at decoded row {i}")
        try: rr=int(float(str(r).strip()))
        except Exception as e: raise RuntimeError(f"SOURCE_ROW_INVALID:{r!r}") from e
        if not str(n or "").strip(): raise RuntimeError(f"BUSINESS_NAME_MISSING source_row={rr}")
        out.append({"r":rr,"n":str(n).strip(),"p":str(p or "").strip(),"a":str(a or "").strip(),"ph":str(ph or "").strip(),"em":str(em or "").strip(),"cow":str(cow or "").strip()})
    return out


def parse_text(raw: bytes):
    text=raw.decode("utf-8-sig")
    s=text.lstrip()
    if s.startswith("["):
        obj=json.loads(text)
        if isinstance(obj,list): return obj
    if s.startswith("{"):
        try:
            obj=json.loads(text)
            if isinstance(obj,list): return obj
            if isinstance(obj,dict):
                for k in ("rows","records","data","items"):
                    if isinstance(obj.get(k),list): return obj[k]
        except json.JSONDecodeError: pass
    lines=[x for x in text.splitlines() if x.strip()]
    if lines:
        try:
            vals=[json.loads(x) for x in lines]
            if vals and all(isinstance(x,dict) for x in vals): return vals
        except Exception: pass
    try:
        dialect=csv.Sniffer().sniff(text[:10000],delimiters=",;\t|")
        vals=list(csv.DictReader(io.StringIO(text),dialect=dialect))
        if vals: return vals
    except Exception: pass
    raise RuntimeError("DECODED_SNAPSHOT_FORMAT_UNSUPPORTED")


def unique_rows(rows):
    by={}
    for row in canonicalize(rows):
        r=row["r"]
        if r in by and by[r]!=row: raise RuntimeError(f"CONFLICTING_DUPLICATE_SOURCE_ROW:{r}")
        by[r]=row
    return [by[k] for k in sorted(by)]


def decode_payload(parts):
    joined="".join(compact_text(p) for p in parts)
    # Historical chunks were emitted by multiple writers; accept both standard and
    # url-safe base64 alphabets. Compression integrity + exact 5047 count remain the gate.
    joined=re.sub(r"[^A-Za-z0-9+/=_-]","",joined)
    joined += "="*((4-len(joined)%4)%4)
    packed=base64.b64decode(joined,altchars=b"-_",validate=False)
    tries=[("bz2",bz2.decompress),("gzip",gzip.decompress)]
    errors=[]
    for codec,fn in tries:
        try:
            raw=fn(packed)
            return unique_rows(parse_text(raw)),codec,packed,raw
        except Exception as e: errors.append(f"{codec}:{type(e).__name__}:{e}")
    try:
        return unique_rows(parse_text(packed)),"raw",packed,packed
    except Exception as e:
        errors.append(f"raw:{type(e).__name__}:{e}")
        raise RuntimeError(" | ".join(errors))


def decode_snapshot(input_dir: Path, expected: int):
    parts=sorted(input_dir.glob("part_*.b64"))
    if not parts: raise RuntimeError(f"NO_SNAPSHOT_PARTS:{input_dir}")

    numeric=[p for p in parts if re.fullmatch(r"part_\d+\.b64",p.name)]
    prefix_filtered=[]
    for p in parts:
        s=compact_text(p)
        if any(p!=q and len(s)<len(compact_text(q)) and compact_text(q).startswith(s) for q in parts):
            continue
        prefix_filtered.append(p)

    candidates=[]
    for label,ps in (("numeric_primary",numeric),("prefix_filtered",prefix_filtered),("all_parts",parts)):
        names=[p.name for p in ps]
        if ps and names not in [x[1] for x in candidates]: candidates.append((label,names,ps))

    diagnostics=[]
    for label,names,ps in candidates:
        try:
            rows,codec,packed,raw=decode_payload(ps)
            diagnostics.append({"candidate":label,"parts":names,"rows":len(rows),"codec":codec})
            if len(rows)==expected:
                return rows,{
                    "candidate":label,
                    "parts":[{"file":p.name,"sha256":hashlib.sha256(p.read_bytes()).hexdigest(),"chars":len(compact_text(p))} for p in ps],
                    "packed_sha256":hashlib.sha256(packed).hexdigest(),
                    "decoded_sha256":hashlib.sha256(raw).hexdigest(),
                    "codec":codec,
                    "diagnostics":diagnostics,
                }
        except Exception as e:
            diagnostics.append({"candidate":label,"parts":names,"error":f"{type(e).__name__}:{e}"})
    raise RuntimeError("NO_EXACT_5047_SNAPSHOT:"+json.dumps(diagnostics,separators=(",",":")))


def write_queue(path: Path, rows):
    raw=("\n".join(json.dumps(x,ensure_ascii=False,separators=(",",":")) for x in rows)+"\n").encode()
    enc=base64.b64encode(gzip.compress(raw,9)).decode()
    path.write_text(enc+"\n",encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--input-dir",required=True)
    ap.add_argument("--outdir",required=True)
    ap.add_argument("--expected",type=int,required=True)
    ap.add_argument("--shards",type=int,default=12)
    a=ap.parse_args()

    rows,source_meta=decode_snapshot(Path(a.input_dir),a.expected)
    if len(rows)!=a.expected: raise SystemExit(f"SNAPSHOT_COUNT_MISMATCH expected={a.expected} got={len(rows)}")
    d=Path(a.outdir); d.mkdir(parents=True,exist_ok=True)
    for old in d.glob("queue_*.jsonl.gz.b64"): old.unlink()
    shards=[]
    for i in range(a.shards):
        shard=rows[i::a.shards]
        p=d/f"queue_{i:02d}.jsonl.gz.b64"
        shards.append({"file":p.name,"records":len(shard),"sha256":write_queue(p,shard)})
    manifest={"schema":"gws-legacy-immutable-snapshot-v2","expected_records":a.expected,"unique_records":len(rows),"source":source_meta,"shards":shards}
    (d/"manifest.json").write_text(json.dumps(manifest,indent=2)+"\n",encoding="utf-8")
    print("SNAPSHOT_MATERIALIZED="+json.dumps({"expected":a.expected,"unique":len(rows),"candidate":source_meta["candidate"],"codec":source_meta["codec"],"parts":[x["file"] for x in source_meta["parts"]],"shards":len(shards)},separators=(",",":")))


if __name__=="__main__": main()
