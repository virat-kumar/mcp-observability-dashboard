"""Idempotent live migration of legacy request/telemetry/capture history.

Never changes the source DB or MCP servers. Keeps v2-only values when populated.
"""
import os
import sqlite3
from pathlib import Path
source=Path(os.environ.get('LEGACY_OBS_DB','/home/virat/Projects/mcp-observability-dashboard/data/observability.db'))
dest=Path(os.environ.get('V2_OBS_DB',str(Path(__file__).resolve().parents[1]/'data/inspector/observability.db')))
if not source.is_file() or not dest.is_file():
    raise SystemExit('Both existing databases are required')
conn=sqlite3.connect(dest,timeout=60,uri=True)
try:
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=60000')
    conn.execute('ATTACH DATABASE ? AS legacy',(f'file:{source}?mode=ro',))
    before={t:conn.execute(f'SELECT count(*) FROM {t}').fetchone()[0] for t in ('requests','events','captures')}
    for name in ('requests','events','captures'):
        conn.execute(f'INSERT OR IGNORE INTO "{name}" SELECT * FROM legacy."{name}"')
        conn.commit()
    # Newly collected v2 lifecycle rows may predate old capture association; fill those blanks.
    for col in ('tool_name','input_json','output_json','capture_source','payload_captured_at',
                'mcp_reply_at','delivered_at','duration_ms','reply_to_delivery_ms','timing_confidence'):
        conn.execute(f'''UPDATE requests SET {col}=(SELECT l.{col} FROM legacy.requests l WHERE l.request_key=requests.request_key)
            WHERE {col} IS NULL AND EXISTS (SELECT 1 FROM legacy.requests l
                WHERE l.request_key=requests.request_key AND l.{col} IS NOT NULL)''')
    conn.commit()
    after={t:conn.execute(f'SELECT count(*) FROM {t}').fetchone()[0] for t in ('requests','events','captures')}
    conn.execute('DETACH DATABASE legacy')
    print('Synchronized original history read-only',{'before':before,'after':after})
finally:
    conn.close()
