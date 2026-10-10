"""Verify on-demand legacy health-history preservation without high-frequency polling."""
import json
import sqlite3
import urllib.parse
import urllib.request
from pathlib import Path

URL='http://127.0.0.1:9120/api/snapshots'
OLD=Path(__file__).resolve().parents[2]/'data/observability.db'
FIELDS='ts,mcp,healthy,ready,tool_calls_total,poll_errors_total,queue_length,queue_capacity,worker_occupancy,worker_capacity,rss_bytes,net_rx_bytes,net_tx_bytes'

def get(**params):
    with urllib.request.urlopen(URL+'?'+urllib.parse.urlencode(params), timeout=8) as r:
        assert r.status==200
        return json.load(r)['rows']

with sqlite3.connect(f'file:{OLD}?mode=ro',uri=True) as db:
    db.row_factory=sqlite3.Row
    cutoff='2026-10-09T00:00:00+00:00'
    for name in ('terminal','playwright','computer','personal','excel','background'):
        expect=[dict(x) for x in db.execute(f'SELECT {FIELDS} FROM snapshots WHERE mcp=? AND ts<=? ORDER BY ts DESC LIMIT 20',(name,cutoff)).fetchall()]
        actual=get(mcp=name,to='2026-10-09T00:00:00Z',limit=20)
        assert actual==expect, f'historical mismatch {name}: {len(actual)} vs {len(expect)}'
        print(name,'historical_rows',len(actual),'exact_match',True)
    latest=get(mcp='all',limit=20)
    assert len(latest)==20
    times=[x['ts'] for x in latest]
    assert times==sorted(times,reverse=True),'merged snapshots not sorted newest-first'
    earliest=get(mcp='terminal',to='2026-10-08T12:00:00Z',limit=3)
    assert len(earliest)==3
    print('all MCP historical snapshots preserved; newest and older date filters pass')

with urllib.request.urlopen('http://127.0.0.1:9120/api/config') as response:
    config=json.load(response)
assert {k:v['listen_port'] for k,v in config['http_capture'].items()}=={'terminal':18900,'excel':18017,'personal':18765,'background':17874}
print('Gateway capture configuration parity passed')
