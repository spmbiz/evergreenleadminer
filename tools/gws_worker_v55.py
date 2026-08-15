#!/usr/bin/env python3
"""Live-visible wrapper around the checkpointed v5.4 worker.

Keeps all v5.4 semantics unchanged, but mirrors every durable local checkpoint as
a GitHub Actions notice annotation. This makes worker progress queryable through
the Checks API while a job is still running.
"""
from __future__ import annotations

import json

import gws_worker_v54 as _base

_original_checkpoint = _base._checkpoint


def _escape_workflow_command(text: str) -> str:
    return str(text).replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')


def _checkpoint(core, d, part, out, **kwargs):
    _original_checkpoint(core, d, part, out, **kwargs)
    try:
        progress = json.loads((d / 'progress.json').read_text(encoding='utf-8'))
        compact = {
            'worker': progress.get('worker'),
            'stage': progress.get('stage'),
            'batch': progress.get('batch_index'),
            'finalized': progress.get('finalized_rows'),
            'partition': progress.get('partition_size'),
            'pending_total': progress.get('pending_total'),
            'statuses': progress.get('statuses'),
            'elapsed_seconds': progress.get('elapsed_seconds'),
        }
        msg = _escape_workflow_command(json.dumps(compact, separators=(',', ':')))
        print(f"::notice title=GWS worker {progress.get('worker')} progress::{msg}", flush=True)
    except Exception as exc:
        print(f"::warning title=GWS progress annotation failed::{type(exc).__name__}", flush=True)


_base._checkpoint = _checkpoint
worker = _base.worker
