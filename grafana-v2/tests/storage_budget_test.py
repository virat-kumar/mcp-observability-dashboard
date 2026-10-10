"""Storage retention tests on disposable fixtures only; never touch production DB."""
import importlib.util
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT=Path(__file__).resolve().parents[1]/'scripts/enforce_storage_budget.py'
spec=importlib.util.spec_from_file_location('storage_budget',SCRIPT)
mod=importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
with tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp)/'data'
    path=root/'inspector'/'observability.db'
    path.parent.mkdir(parents=True)
    cap=Path(tmp)/'old-capture-writer-path';cap.mkdir()
    db=sqlite3.connect(path)
    db.executescript('''
       CREATE TABLE snapshots(id INTEGER PRIMARY KEY,ts TEXT);
       CREATE TABLE events(id INTEGER PRIMARY KEY,ts TEXT);
       CREATE TABLE requests(request_key TEXT PRIMARY KEY, observed_at TEXT);
       CREATE TABLE captures(capture_key TEXT PRIMARY KEY,request_at TEXT,response_at TEXT);
       CREATE TABLE capture_offsets(path TEXT PRIMARY KEY,byte_offset INTEGER);
    ''')
    old=(datetime.now(timezone.utc)-timedelta(days=40)).isoformat()
    recent=datetime.now(timezone.utc).isoformat()
    for row in range(15):
        db.execute('INSERT INTO snapshots VALUES (?,?)',(row,old if row<6 else recent))
        db.execute('INSERT INTO events VALUES (?,?)',(row,old if row<6 else recent))
        db.execute('INSERT INTO requests VALUES (?,?)',(f'k{row}',old if row<6 else recent))
        db.execute('INSERT INTO captures VALUES (?,?,?)',(f'k{row}',old if row<6 else recent,old if row<6 else recent))
    db.commit()
    # The cleanup runner skips a logfile until the collector consumed all bytes.
    l=cap/'computer.jsonl';l.write_bytes(b'a'*mod.CAPTURE_ROTATE_BYTES)
    db.execute('INSERT INTO capture_offsets VALUES(?,?)',('/data/captures/computer.jsonl',l.stat().st_size-1));db.commit();db.close()
    x=mod.prune(path,root,cap,trim_at=mod.TRIM_AT,budget=mod.BUDGET)
    assert x['maintenance']=='ok',x
    assert l.stat().st_size==mod.CAPTURE_ROTATE_BYTES,'unread raw data must not be truncated'
    with sqlite3.connect(path) as check:
        for t in mod.TABLES:
            assert check.execute('SELECT count(*) FROM '+t).fetchone()[0]==9,(t,x)
        check.execute('UPDATE capture_offsets SET byte_offset=?',(l.stat().st_size,));check.commit()
    y=mod.prune(path,root,cap,trim_at=mod.TRIM_AT,budget=mod.BUDGET)
    assert y['rotated']=={'computer.jsonl':mod.CAPTURE_ROTATE_BYTES},y
    assert l.stat().st_size==0
    # Freed SQLite pages must count as reusable capacity, not force deletion of
    # every row just because physical allocation remains stable.
    with sqlite3.connect(path) as check:
        assert check.execute('SELECT count(*) FROM requests').fetchone()[0]==9
print('PASS: time retention, unread capture preservation, log rotation, bounded active data')

# Exercise size-pressure cleanup with a throwaway SQLite fixture. In this test
# the database file stays allocated while its freed pages become reusable.
with tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp)/'data';root.mkdir()
    folder=root/'inspector';folder.mkdir()
    path=folder/'observability.db'
    capture=Path(tmp)/'capture';capture.mkdir()
    db=sqlite3.connect(path)
    db.executescript("""
    CREATE TABLE snapshots(id INTEGER PRIMARY KEY,ts TEXT);
    CREATE TABLE events(id INTEGER PRIMARY KEY,ts TEXT);
    CREATE TABLE requests(request_key TEXT PRIMARY KEY,observed_at TEXT);
    CREATE TABLE captures(capture_key TEXT PRIMARY KEY,request_at TEXT,response_at TEXT,request_json TEXT);
    CREATE TABLE capture_offsets(path TEXT PRIMARY KEY,byte_offset INTEGER);
    """)
    recent=datetime.now(timezone.utc).isoformat()
    for n in range(220):
        db.execute('INSERT INTO captures VALUES (?,?,?,?)',
                   (f'big-{n:03d}',recent,recent,'X'*65000))
    db.commit();db.close()
    initial=mod.bytes_used(root)
    assert initial>11*1024*1024,initial
    result=mod.prune(path,root,capture,trim_at=10*1024*1024,budget=18*1024*1024)
    assert result['removed']['captures']>0,result
    with sqlite3.connect(path) as c:
        count=c.execute('SELECT COUNT(*) FROM captures').fetchone()[0]
        assert 100<=count<220,count
        assert mod.effective_usage(c,root,capture)<10*1024*1024
    print('PASS: pressure evicts oldest data, retains recent minimum, reuses SQLite pages')
