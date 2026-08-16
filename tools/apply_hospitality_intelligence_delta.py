#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import json
import os
import sqlite3
from pathlib import Path

INTEL_RAW = (
    'account_id', 'intelligence_business_type', 'intelligence_fit_decision', 'intelligence_confidence', 'intelligence_status',
    'pms_fingerprints', 'property_count_known', 'sample_property_urls', 'contactability_score', 'portfolio_leverage_score',
    'commercial_score', 'intelligence_processed_at'
)


def canonical_health(con: sqlite3.Connection) -> tuple[int, str]:
    exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='leads'").fetchone()
    if not exists:
        return 0, 'missing_leads_table'
    count = int(con.execute('SELECT COUNT(*) FROM leads').fetchone()[0])
    integrity = str(con.execute('PRAGMA integrity_check').fetchone()[0])
    return count, integrity


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--canonical-db', required=True)
    ap.add_argument('--delta', required=True)
    ap.add_argument('--out-summary', required=True)
    a = ap.parse_args()

    # Production canonical is always named hospitality-canonical.sqlite. Tests
    # and isolated fixtures can opt into floor=0 or use another filename.
    default_floor = 10000 if Path(a.canonical_db).name == 'hospitality-canonical.sqlite' else 0
    safety_floor = int(os.environ.get('HOSPITALITY_CANONICAL_SAFETY_FLOOR', str(default_floor)))

    con = sqlite3.connect(a.canonical_db)
    con.row_factory = sqlite3.Row
    before_count, before_integrity = canonical_health(con)
    if before_integrity != 'ok' or before_count < safety_floor:
        con.close()
        raise SystemExit(
            f'REFUSING_V2_CANONICAL_MERGE: rows={before_count} integrity={before_integrity} floor={safety_floor}'
        )

    seen = updated = missing = email_filled = instagram_filled = 0
    with gzip.open(a.delta, 'rt', encoding='utf-8') as f:
        for line in f:
            if not line.strip():
                continue
            d = json.loads(line)
            domain = str(d.get('domain') or '').strip().lower()
            if not domain:
                continue
            seen += 1
            row = con.execute('SELECT public_email,instagram,raw_json FROM leads WHERE domain=?', (domain,)).fetchone()
            if not row:
                missing += 1
                continue
            try:
                raw = json.loads(row['raw_json'] or '{}')
            except Exception:
                raw = {}
            dirty = False
            public_email = str(row['public_email'] or '')
            instagram = str(row['instagram'] or '')
            new_email = str(d.get('public_email') or '').strip()
            new_ig = str(d.get('instagram') or '').strip()
            if not public_email and new_email:
                public_email = new_email
                raw['public_email'] = new_email
                raw['email_source_url'] = d.get('public_email_source_url') or raw.get('email_source_url') or ''
                email_filled += 1
                dirty = True
            if not instagram and new_ig:
                instagram = new_ig
                raw['instagram'] = new_ig
                raw['instagram_source_url'] = new_ig
                instagram_filled += 1
                dirty = True
            for key in ('facebook', 'whatsapp', 'contact_page', 'portfolio_url'):
                value = d.get(key)
                if value and not str(raw.get(key) or '').strip():
                    raw[key] = value
                    if key == 'facebook':
                        raw['facebook_source_url'] = value
                    dirty = True
            for key in INTEL_RAW:
                if key in d and raw.get(key) != d.get(key):
                    raw[key] = d.get(key)
                    dirty = True
            if dirty:
                con.execute('UPDATE leads SET public_email=?,instagram=?,raw_json=? WHERE domain=?',
                            (public_email, instagram, json.dumps(raw, ensure_ascii=False), domain))
                updated += 1
    con.commit()

    after_count, after_integrity = canonical_health(con)
    if after_integrity != 'ok' or after_count != before_count or after_count < safety_floor:
        con.rollback()
        con.close()
        raise SystemExit(
            f'REFUSING_V2_CANONICAL_PERSIST: before={before_count} after={after_count} '
            f'integrity={after_integrity} floor={safety_floor}'
        )
    con.close()

    summary = {
        'delta_rows': seen,
        'canonical_rows_updated': updated,
        'canonical_missing': missing,
        'public_emails_filled': email_filled,
        'instagram_filled': instagram_filled,
        'canonical_rows_before': before_count,
        'canonical_rows_after': after_count,
        'canonical_integrity': after_integrity,
        'canonical_safety_floor': safety_floor,
    }
    p = Path(a.out_summary)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(summary))


if __name__ == '__main__':
    main()
