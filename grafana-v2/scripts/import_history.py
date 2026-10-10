"""One-time safe history migration; reads legacy DB read-only and writes a new DB."""
import os
import sqlite3
from pathlib import Path

source = Path(os.environ.get('LEGACY_OBS_DB', '/home/virat/Projects/mcp-observability-dashboard/data/observability.db'))
dest = Path(os.environ.get('V2_OBS_DB', str(Path(__file__).resolve().parents[1] / 'data/inspector/observability.db')))
if dest.exists():
    raise SystemExit(f'Refusing to overwrite existing {dest}')
dest.parent.mkdir(parents=True,exist_ok=True)
conn = sqlite3.connect(dest, uri=True)
try:
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('ATTACH DATABASE ? AS legacy', (f'file:{source}?mode=ro',))
    tables = ['snapshots','events','requests','collector_state','captures','capture_offsets']
    for name in tables:
        schema = conn.execute('SELECT sql FROM legacy.sqlite_master WHERE type="table" AND name=?',(name,)).fetchone()
        if schema:
            conn.execute(schema[0]);conn.commit()
            if name == 'snapshots':
                conn.execute('INSERT INTO snapshots SELECT * FROM legacy.snapshots WHERE 0')
            else:
                conn.execute(f'INSERT INTO "{name}" SELECT * FROM legacy."{name}"')
            conn.commit()
            print(name,conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0],flush=True)
    for (ddl,) in conn.execute('SELECT sql FROM legacy.sqlite_master WHERE type="index" AND sql IS NOT NULL').fetchall():
        conn.execute(ddl)
    conn.execute('CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON snapshots(ts)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_requests_pending_reply ON requests(mcp,reply_to_delivery_ms) WHERE reply_to_delivery_ms IS NULL')
    conn.commit()
    conn.execute('DETACH DATABASE legacy')
finally:
    conn.close()
print('History migration complete:',dest.stat().st_size, 'bytes',flush=True)
