#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00', 'Z')


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def sha256_text(value: str) -> str:
    return hashlib.sha256((value or '').encode('utf-8', 'ignore')).hexdigest()


def material_snapshot(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    d = dict(row)
    try:
        raw = json.loads(d.get('raw_json') or '{}')
    except Exception:
        raw = {}
    return {
        'domain': d.get('domain') or '',
        'name': d.get('name') or '',
        'country': d.get('country') or '',
        'region': d.get('region') or '',
        'city': d.get('city') or '',
        'street': d.get('street') or '',
        'website': d.get('website') or '',
        'public_email': d.get('public_email') or '',
        'public_phone': d.get('public_phone') or '',
        'instagram': d.get('instagram') or raw.get('instagram') or '',
        'facebook': raw.get('facebook') or '',
        'whatsapp': raw.get('whatsapp') or '',
        'contact_page': raw.get('contact_page') or '',
        'portfolio_url': raw.get('portfolio_url') or '',
        'operator_score': int(d.get('operator_score') or 0),
        'premium_score': int(d.get('premium_score') or 0),
        'fit_tier': d.get('fit_tier') or '',
        'live_status': d.get('live_status') or '',
    }


def material_hash(row: sqlite3.Row | dict[str, Any]) -> str:
    return sha256_text(stable_json(material_snapshot(row)))


def init_db(con: sqlite3.Connection) -> None:
    con.executescript(
        '''
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS meta(
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS accounts(
          account_id TEXT PRIMARY KEY,
          domain TEXT UNIQUE NOT NULL,
          name TEXT,
          country TEXT,
          region TEXT,
          city TEXT,
          website TEXT,
          first_seen TEXT,
          last_seen TEXT,
          material_hash TEXT,
          last_material_change_at TEXT,
          last_attempt_at TEXT,
          last_classified_at TEXT,
          classifier_model TEXT,
          classifier_prompt_version TEXT,
          entity_match TEXT,
          business_type TEXT,
          fit_decision TEXT,
          confidence REAL,
          unusual_or_novel INTEGER DEFAULT 0,
          public_email TEXT,
          public_phone TEXT,
          instagram TEXT,
          facebook TEXT,
          whatsapp TEXT,
          contact_page TEXT,
          portfolio_url TEXT,
          pms_fingerprints_json TEXT,
          property_count_known INTEGER DEFAULT 0,
          portfolio_hash TEXT,
          sample_property_urls_json TEXT,
          contactability_score INTEGER DEFAULT 0,
          portfolio_leverage_score INTEGER DEFAULT 0,
          commercial_score INTEGER DEFAULT 0,
          status TEXT,
          last_error TEXT,
          raw_json TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_accounts_status ON accounts(status);
        CREATE INDEX IF NOT EXISTS idx_accounts_fit ON accounts(fit_decision, confidence);
        CREATE INDEX IF NOT EXISTS idx_accounts_last_attempt ON accounts(last_attempt_at);

        CREATE TABLE IF NOT EXISTS assets(
          asset_id TEXT PRIMARY KEY,
          account_id TEXT NOT NULL,
          property_name TEXT,
          url TEXT NOT NULL,
          source_type TEXT,
          first_seen TEXT,
          last_seen TEXT,
          content_hash TEXT,
          sample_priority INTEGER DEFAULT 0,
          raw_json TEXT,
          FOREIGN KEY(account_id) REFERENCES accounts(account_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_assets_account ON assets(account_id);
        CREATE INDEX IF NOT EXISTS idx_assets_priority ON assets(account_id, sample_priority DESC);

        CREATE TABLE IF NOT EXISTS search_results(
          search_id TEXT PRIMARY KEY,
          account_id TEXT NOT NULL,
          query_family TEXT,
          query_text TEXT,
          provider TEXT,
          rank INTEGER,
          title TEXT,
          url TEXT,
          snippet TEXT,
          retrieved_at TEXT,
          status TEXT,
          error TEXT,
          raw_json TEXT,
          FOREIGN KEY(account_id) REFERENCES accounts(account_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_search_account ON search_results(account_id);
        CREATE INDEX IF NOT EXISTS idx_search_yield ON search_results(provider, query_family, status);

        CREATE TABLE IF NOT EXISTS query_yield(
          provider TEXT NOT NULL,
          query_family TEXT NOT NULL,
          queries INTEGER DEFAULT 0,
          successes INTEGER DEFAULT 0,
          errors INTEGER DEFAULT 0,
          results INTEGER DEFAULT 0,
          useful_results INTEGER DEFAULT 0,
          updated_at TEXT,
          PRIMARY KEY(provider, query_family)
        );

        CREATE TABLE IF NOT EXISTS runs(
          run_id TEXT PRIMARY KEY,
          started_at TEXT,
          finished_at TEXT,
          accounts_planned INTEGER DEFAULT 0,
          accounts_processed INTEGER DEFAULT 0,
          qwen_classified INTEGER DEFAULT 0,
          qwen_unavailable INTEGER DEFAULT 0,
          search_queries INTEGER DEFAULT 0,
          assets_found INTEGER DEFAULT 0,
          errors INTEGER DEFAULT 0,
          metrics_json TEXT
        );
        '''
    )
    con.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)', ('schema_version', str(SCHEMA_VERSION)))
    con.commit()


def connect(path: str | Path) -> sqlite3.Connection:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p)
    con.row_factory = sqlite3.Row
    init_db(con)
    return con


def parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except Exception:
        return None


def retry_due(last_attempt_at: str | None, hours: int, now: dt.datetime | None = None) -> bool:
    if not last_attempt_at:
        return True
    last = parse_iso(last_attempt_at)
    if not last:
        return True
    now = now or dt.datetime.now(dt.timezone.utc)
    return now - last >= dt.timedelta(hours=max(1, hours))


def stale_due(last_classified_at: str | None, days: int, now: dt.datetime | None = None) -> bool:
    if not last_classified_at:
        return True
    last = parse_iso(last_classified_at)
    if not last:
        return True
    now = now or dt.datetime.now(dt.timezone.utc)
    return now - last >= dt.timedelta(days=max(1, days))


def account_id_for_domain(domain: str) -> str:
    return 'acct:' + (domain or '').strip().lower()


def asset_id_for_url(account_id: str, url: str) -> str:
    return 'asset:' + sha256_text(f'{account_id}|{url.strip()}')[:32]


def search_id_for_result(account_id: str, provider: str, query_family: str, query_text: str, url: str, rank: int) -> str:
    return 'search:' + sha256_text('|'.join([account_id, provider, query_family, query_text, url, str(rank)]))[:32]
