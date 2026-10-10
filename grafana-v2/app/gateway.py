"""MCP-safe standalone capture gateway, independent from dashboards/collectors.

Only forwards MCP HTTP traffic to configured upstreams. No MCP server changes.
"""
import asyncio
import os
import sqlite3
from pathlib import Path
from .capture import CaptureProxyManager, ensure_schema

async def main():
    db_path=Path(os.environ.get('MCP_OBS_DB','/data/observability.db'))
    db_path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(db_path, timeout=30, isolation_level='DEFERRED')
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA busy_timeout=30000')
    ensure_schema(db)
    proxy=CaptureProxyManager(db)
    await proxy.start()
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await proxy.close()
        db.close()

if __name__=='__main__':
    asyncio.run(main())
