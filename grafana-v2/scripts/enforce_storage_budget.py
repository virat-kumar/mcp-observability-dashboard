#!/usr/bin/env python3
"""Bound MCP Observatory v2 storage without touching or restarting any MCP server.

Data policy: retain 48h snapshots, 14d events/captures, 30d requests.
At 2.2 GiB active data usage, aggressively evict oldest telemetry to stay
below the 3 GiB operating budget. Prometheus enforces its own TSDB size limit.

This is a scheduled retention controller, not an ext4 hard quota.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parents[1] / 'data'
DEFAULT_CAPTURE_DIR = Path(__file__).resolve().parents[2] / 'data' / 'captures'
CAPTURE_ROTATE_BYTES = 32 * 1024 * 1024
GIB = 1024 ** 3
# Keep ample reserve for WAL, Grafana/Prometheus growth, and write bursts.
TRIM_AT = int(2.2 * GIB)
BUDGET = 3 * GIB
TABLES = ('captures', 'events', 'requests', 'snapshots')
MIN_KEEP = {'captures': 100, 'events': 1000, 'requests': 100, 'snapshots': 100}
BATCH = 2500


def bytes_used(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for base, _dirs, files in os.walk(path):
        for filename in files:
            f = Path(base) / filename
            try:
                total += f.stat().st_blocks * 512
            except FileNotFoundError:
                pass
    return total


def cutoff(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def delete_batch(db: sqlite3.Connection, table: str, order: str, batch: int = BATCH) -> int:
    pk = {'captures': 'capture_key', 'events': 'id', 'requests': 'request_key', 'snapshots': 'id'}[table]
    available = max(0, db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] - MIN_KEEP[table])
    if available == 0:
        return 0
    db.execute(f'DELETE FROM {table} WHERE {pk} IN (SELECT {pk} FROM {table} ORDER BY {order} ASC LIMIT ?)', (min(batch, available),))
    return db.execute('SELECT changes()').fetchone()[0]


def rotate_ingested_logs(db: sqlite3.Connection, capture_dir: Path) -> dict[str, int]:
    """Truncate already-ingested JSONL logs in-place so MCP wrapper FDs keep working.

    The producer does not change its file path or fd, and capture_offsets' prior
    high-water mark is automatically reset by ingest_stdio_files when file shrinks.
    """
    rotated = {}
    for log in capture_dir.glob('*.jsonl'):
        if not log.is_file():
            continue
        size = log.stat().st_size
        if size < CAPTURE_ROTATE_BYTES:
            continue
        # Offsets are stored in container-mount namespace, not host namespace.
        offset_path = '/data/captures/' + log.name
        row = db.execute('SELECT byte_offset FROM capture_offsets WHERE path=?', (offset_path,)).fetchone()
        if row is None or row[0] < size:
            continue  # Never discard bytes not yet captured by the inspector.
        with log.open('r+b') as handle:
            if os.fstat(handle.fileno()).st_size != size:
                continue
            handle.truncate(0)
        rotated[log.name] = size
    return rotated


def data_usage(root: Path, capture_dir: Path) -> int:
    return bytes_used(root) + bytes_used(capture_dir)


def effective_usage(db: sqlite3.Connection, root: Path, capture_dir: Path) -> int:
    # Freed SQLite pages remain physically allocated but are reused before
    # database growth; subtract them when deciding whether to delete more rows.
    page_size = int(db.execute('PRAGMA page_size').fetchone()[0])
    free_pages = int(db.execute('PRAGMA freelist_count').fetchone()[0])
    return max(0, data_usage(root, capture_dir) - free_pages * page_size)


def prune(db_path: Path, root: Path, capture_dir: Path, trim_at: int, budget: int) -> dict:
    before = data_usage(root, capture_dir)
    result = {'before_bytes': before, 'budget_bytes': budget, 'trim_at_bytes': trim_at,
              'removed': {t: 0 for t in TABLES}, 'rotated': {}, 'maintenance': 'ok'}
    if not db_path.is_file():
        result['maintenance'] = 'database_missing'
        result['after_bytes'] = before
        return result
    db = sqlite3.connect(db_path, timeout=5, isolation_level=None)
    try:
        db.execute('PRAGMA busy_timeout=5000')
        result['rotated'] = rotate_ingested_logs(db, capture_dir)
        # Indexes make time-based pruning cheap; CREATE only on fresh DB once.
        for statement in (
            'CREATE INDEX IF NOT EXISTS idx_events_ts_budget ON events(ts)',
            'CREATE INDEX IF NOT EXISTS idx_captures_at_budget ON captures(request_at)',
            'CREATE INDEX IF NOT EXISTS idx_requests_observed_budget ON requests(observed_at)',
        ):
            db.execute(statement)
        rules = (
            ('snapshots', 'ts', 2),
            ('events', 'ts', 14),
            ('captures', 'request_at', 14),
            ('requests', 'observed_at', 30),
        )
        for table, field, days in rules:
            threshold = cutoff(days)
            # Bounded batches avoid long write locks against live MCP capture.
            for _ in range(60):
                db.execute('BEGIN IMMEDIATE')
                try:
                    pk = {'captures':'capture_key', 'events':'id', 'requests':'request_key', 'snapshots':'id'}[table]
                    db.execute(
                        f'DELETE FROM {table} WHERE {pk} IN '
                        f'(SELECT {pk} FROM {table} WHERE {field} < ? LIMIT ?)',
                        (threshold, BATCH),
                    )
                    n = db.execute('SELECT changes()').fetchone()[0]
                    db.execute('COMMIT')
                except Exception:
                    db.execute('ROLLBACK')
                    raise
                result['removed'][table] += n
                if n < BATCH:
                    break
        # Prune the oldest high-cardinality records when the data budget is hit.
        used = effective_usage(db, root, capture_dir)
        if used >= trim_at and data_usage(root, capture_dir) - db_path.stat().st_size >= trim_at:
            result['maintenance'] = 'non_sqlite_data_over_budget'
        elif used >= trim_at:
            # Aged capture + request payload data are usually the largest files.
            for table, order in (
                ('captures','COALESCE(response_at, request_at)'),
                ('requests','observed_at'),
                ('events','ts'),
                ('snapshots','ts'),
            ):
                # Delete in bounded chunks. SQLite reuses freed pages on future
                # writes, even when the database's allocated size does not shrink.
                for _ in range(100):
                    db.execute('BEGIN IMMEDIATE')
                    try:
                        n = delete_batch(db, table, order)
                        db.execute('COMMIT')
                    except Exception:
                        db.execute('ROLLBACK')
                        raise
                    result['removed'][table] += n
                    if n == 0:
                        break
                    # Check reclaimed *logical* bytes after each batch; this
                    # avoids deleting more recent records than necessary.
                    used = effective_usage(db, root, capture_dir)
                    if used < trim_at:
                        break
                if used < trim_at:
                    break
        if used >= trim_at and result['maintenance'] == 'ok':
            result['maintenance'] = 'near_budget_with_minimum_records_preserved'
        try:
            db.execute('PRAGMA wal_checkpoint(PASSIVE)')
        except sqlite3.Error:
            pass
    except sqlite3.Error as e:
        result['maintenance'] = f'sqlite_error:{type(e).__name__}'
    finally:
        db.close()
    result['after_bytes'] = data_usage(root, capture_dir)
    # A high-water warning remains visible to the systemd journal / monitoring.
    if result['after_bytes'] >= budget:
        result['maintenance'] = 'OVER_BUDGET'
    return result


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument('--root', type=Path, default=DEFAULT_ROOT)
    p.add_argument('--capture-dir', type=Path, default=DEFAULT_CAPTURE_DIR)
    p.add_argument('--trim-at-bytes', type=int, default=TRIM_AT)
    p.add_argument('--budget-bytes', type=int, default=BUDGET)
    args = p.parse_args()
    if args.trim_at_bytes >= args.budget_bytes or args.trim_at_bytes <= 0:
        p.error('trim-at must be positive and smaller than the budget')
    root = args.root.resolve()
    report = prune(root/'inspector'/'observability.db', root, args.capture_dir.resolve(),
                   args.trim_at_bytes, args.budget_bytes)
    print(json.dumps(report, sort_keys=True))
    return 0 if report['maintenance'] == 'ok' else 2


if __name__ == '__main__':
    sys.exit(main())
