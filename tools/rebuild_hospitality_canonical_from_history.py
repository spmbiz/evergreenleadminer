#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import io
import json
import re
import sqlite3
import tarfile
from pathlib import Path
from urllib.parse import urlparse

MULTI_SUFFIXES = {
    'co.uk','org.uk','me.uk','ltd.uk','plc.uk','net.uk',
    'com.au','net.au','org.au','id.au','asn.au','co.nz','net.nz','org.nz',
    'com.br','net.br','org.br','com.mx','com.ar','com.co','com.pe','com.ec','com.uy','com.py','com.ve',
    'co.za','org.za','net.za','com.sg','com.hk','com.tr','com.gr','com.cy','com.mt',
    'co.cr','com.pa','com.do','com.gt','com.hn','com.sv','com.ni','co.il','com.my','co.th','com.ph','com.tw','com.cn','com.jp','co.jp','ne.jp',
}
MONOTONIC_RAW_FIELDS = (
    'facebook','facebook_source_url','contact_page','whatsapp','portfolio_url',
    'instagram','instagram_source_url','email_source_url',
)
CYCLE_RE = re.compile(r'(20\d{6}T\d{6}Z)')


def root_domain(host: str) -> str:
    h=(host or '').lower().strip().strip('.')
    if h.startswith('www.'):
        h=h[4:]
    if not h:
        return ''
    parts=h.split('.')
    if len(parts)<2:
        return h
    last2='.'.join(parts[-2:])
    if last2 in MULTI_SUFFIXES and len(parts)>=3:
        return '.'.join(parts[-3:])
    return last2


def host(url: str) -> str:
    try:
        return (urlparse(url or '').hostname or '').lower()
    except Exception:
        return ''


def cycle_from_name(name: str) -> str:
    m=CYCLE_RE.search(name)
    return m.group(1) if m else ''


def cycle_iso(cycle: str) -> str:
    try:
        x=dt.datetime.strptime(cycle, '%Y%m%dT%H%M%SZ').replace(tzinfo=dt.timezone.utc)
        return x.isoformat().replace('+00:00','Z')
    except Exception:
        return '1970-01-01T00:00:00Z'


def init_db(con: sqlite3.Connection) -> None:
    con.executescript('''
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS leads(
 domain TEXT PRIMARY KEY,name TEXT,country TEXT,region TEXT,city TEXT,state TEXT,street TEXT,
 website TEXT,public_email TEXT,public_phone TEXT,instagram TEXT,live_status TEXT,fit_tier TEXT,
 operator_score INTEGER,premium_score INTEGER,source_url TEXT,overture_id TEXT,first_seen TEXT,
 last_seen TEXT,source_release TEXT,raw_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_email ON leads(public_email);
CREATE INDEX IF NOT EXISTS idx_phone ON leads(public_phone);
''')


def merge_raw(old_raw: dict, new_raw: dict) -> dict:
    out=dict(new_raw or {})
    for key in MONOTONIC_RAW_FIELDS:
        if not str(out.get(key) or '').strip() and str(old_raw.get(key) or '').strip():
            out[key]=old_raw[key]
    return out


def merge_row(con: sqlite3.Connection, row: dict, seen_at: str) -> tuple[bool,bool]:
    domain=root_domain(str(row.get('domain') or '') or host(str(row.get('website') or '')))
    if not domain:
        return False,False
    old=con.execute('SELECT * FROM leads WHERE domain=?',(domain,)).fetchone()
    oldd=dict(old) if old else {}
    try:
        old_raw=json.loads(oldd.get('raw_json') or '{}') if oldd else {}
    except Exception:
        old_raw={}
    new_raw=merge_raw(old_raw,row)

    first=oldd.get('first_seen') or seen_at
    # Recovery is deliberately monotonic for explicit public contacts. A later
    # partial observation must not erase evidence that existed in history.
    email=str(row.get('public_email') or '') or str(oldd.get('public_email') or '')
    phone=str(row.get('public_phone') or '') or str(oldd.get('public_phone') or '')
    instagram=str(row.get('instagram') or '') or str(oldd.get('instagram') or '')

    values=(
        domain,
        row.get('name') or oldd.get('name'),
        row.get('country') or oldd.get('country'),
        row.get('region') or oldd.get('region'),
        row.get('city') or oldd.get('city'),
        row.get('state') or oldd.get('state'),
        row.get('street') or oldd.get('street'),
        row.get('website') or oldd.get('website'),
        email,phone,instagram,
        row.get('live_status') or oldd.get('live_status'),
        row.get('fit_tier') or oldd.get('fit_tier'),
        int(row.get('operator_score') or oldd.get('operator_score') or 0),
        int(row.get('premium_score') or oldd.get('premium_score') or 0),
        row.get('source_url') or oldd.get('source_url'),
        row.get('overture_id') or oldd.get('overture_id'),
        first,seen_at,
        row.get('release') or row.get('source_release') or oldd.get('source_release'),
        json.dumps(new_raw,ensure_ascii=False),
    )
    con.execute('''INSERT INTO leads(domain,name,country,region,city,state,street,website,public_email,public_phone,instagram,live_status,fit_tier,operator_score,premium_score,source_url,overture_id,first_seen,last_seen,source_release,raw_json)
VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(domain) DO UPDATE SET
 name=excluded.name,country=excluded.country,region=excluded.region,city=excluded.city,state=excluded.state,street=excluded.street,
 website=excluded.website,public_email=excluded.public_email,public_phone=excluded.public_phone,instagram=excluded.instagram,
 live_status=excluded.live_status,fit_tier=excluded.fit_tier,operator_score=excluded.operator_score,premium_score=excluded.premium_score,
 source_url=excluded.source_url,overture_id=excluded.overture_id,last_seen=excluded.last_seen,source_release=excluded.source_release,raw_json=excluded.raw_json''',values)
    return old is None, old is not None


def iter_partition_rows(bundle: Path):
    cycle=cycle_from_name(bundle.name)
    with tarfile.open(bundle,'r:gz') as tf:
        members=sorted((m for m in tf.getmembers() if Path(m.name).name.startswith('partition-') and m.name.endswith('.jsonl.gz')),key=lambda m:m.name)
        for member in members:
            fp=tf.extractfile(member)
            if fp is None:
                continue
            with gzip.GzipFile(fileobj=io.BytesIO(fp.read()),mode='rb') as gz:
                for raw in gz:
                    if not raw.strip():
                        continue
                    try:
                        row=json.loads(raw.decode('utf-8'))
                    except Exception:
                        continue
                    if isinstance(row,dict):
                        yield cycle,row


def main() -> None:
    ap=argparse.ArgumentParser()
    ap.add_argument('--history-root',required=True)
    ap.add_argument('--out-db',required=True)
    ap.add_argument('--cutoff-cycle',required=True,help='Inclusive YYYYMMDDTHHMMSSZ')
    ap.add_argument('--expected-count',type=int,required=True)
    ap.add_argument('--report',required=True)
    a=ap.parse_args()

    bundles=[]
    for p in Path(a.history_root).rglob('hospitality-bundle-*.tar.gz'):
        cycle=cycle_from_name(p.name)
        if cycle and cycle<=a.cutoff_cycle:
            bundles.append((cycle,p))
    bundles.sort(key=lambda x:(x[0],str(x[1])))
    if not bundles:
        raise SystemExit('No immutable Hospitality bundles found before cutoff')

    out=Path(a.out_db)
    out.parent.mkdir(parents=True,exist_ok=True)
    for suffix in ('','-wal','-shm'):
        q=Path(str(out)+suffix)
        if q.exists(): q.unlink()
    con=sqlite3.connect(out)
    con.row_factory=sqlite3.Row
    init_db(con)

    partition_rows=inserted=updated=0
    bundle_rows=[]
    for cycle,bundle in bundles:
        before=con.execute('SELECT COUNT(*) FROM leads').fetchone()[0]
        rows_here=0
        seen_at=cycle_iso(cycle)
        for _,row in iter_partition_rows(bundle):
            rows_here+=1; partition_rows+=1
            new,changed=merge_row(con,row,seen_at)
            inserted+=int(new); updated+=int(changed)
        con.commit()
        after=con.execute('SELECT COUNT(*) FROM leads').fetchone()[0]
        bundle_rows.append({'cycle':cycle,'bundle':bundle.name,'partition_rows':rows_here,'canonical_before':before,'canonical_after':after})

    integrity=con.execute('PRAGMA integrity_check').fetchone()[0]
    total=int(con.execute('SELECT COUNT(*) FROM leads').fetchone()[0])
    emails=int(con.execute("SELECT COUNT(*) FROM leads WHERE public_email IS NOT NULL AND TRIM(public_email)<>''").fetchone()[0])
    instagram=int(con.execute("SELECT COUNT(*) FROM leads WHERE instagram IS NOT NULL AND TRIM(instagram)<>''").fetchone()[0])
    con.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    con.commit(); con.close()

    report={
        'cutoff_cycle':a.cutoff_cycle,
        'expected_count':a.expected_count,
        'canonical_total':total,
        'integrity':integrity,
        'bundles_replayed':len(bundles),
        'partition_rows_replayed':partition_rows,
        'insert_events':inserted,
        'update_events':updated,
        'public_emails':emails,
        'instagram':instagram,
        'last_bundles':bundle_rows[-10:],
    }
    Path(a.report).write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    print(json.dumps(report,indent=2))
    if integrity!='ok':
        raise SystemExit(f'SQLite integrity failed: {integrity}')
    if total!=a.expected_count:
        raise SystemExit(f'Recovery count mismatch: got {total}, expected {a.expected_count}; refusing publish')


if __name__=='__main__':
    main()
