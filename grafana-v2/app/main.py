from __future__ import annotations

import asyncio
import json
import math
import os
import re
import sqlite3
import statistics
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

from .capture import CaptureProxyManager, HTTP_CAPTURE, ensure_schema, ingest_stdio_files, link_captures

DB_PATH = Path(os.getenv("MCP_OBS_DB", "/data/observability.db"))
POLL_SECONDS = float(os.getenv("MCP_OBS_POLL_SECONDS", "0.5"))
ENABLE_DEBUG = os.getenv("MCP_OBS_DEBUG_LOGS", "true").lower() == "true"

DEFAULT_TUNNELS = {
    "terminal": {"name": "Ubuntu Terminal", "port": 8080},
    "playwright": {"name": "Chrome Playwright", "port": 8081},
    "computer": {"name": "Computer Use Linux", "port": 8082},
    "personal": {"name": "Personal MCP", "port": 8083},
    "excel": {"name": "Excel MCP", "port": 8084},
    "background": {"name": "Background Agent", "port": 8085},
}
_raw_tunnels = os.getenv("MCP_OBS_TUNNELS_JSON")
TUNNELS = json.loads(_raw_tunnels) if _raw_tunnels else DEFAULT_TUNNELS
for slug, cfg in TUNNELS.items():
    cfg.setdefault("name", slug)
    if "base" not in cfg:
        if "port" not in cfg:
            raise RuntimeError(f"Tunnel {slug!r} requires either base or port")
        cfg["base"] = f"http://127.0.0.1:{cfg['port']}"
    cfg.setdefault("port", None)

LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:\\.|[^"])*)"')
METRIC_RE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{.*\})?\s+([^\s]+)')

COLLECT_ERRORS = Counter("mcp_observer_collect_errors_total", "Collector errors", ["mcp"])
LAST_COLLECT = Gauge("mcp_observer_last_collect_timestamp_seconds", "Last successful collector timestamp", ["mcp"])
OBSERVED_REQUESTS = Counter("mcp_observer_requests_observed_total", "Completed MCP requests observed", ["mcp", "status"])
OBSERVED_LATENCY = Histogram("mcp_observer_request_latency_seconds", "Observed MCP end-to-end request latency", ["mcp"])
TUNNEL_HEALTH = Gauge("mcp_observer_tunnel_health", "Tunnel health probe", ["mcp", "probe"])


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_dt(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        d = datetime.fromisoformat(v)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc).isoformat()
    except Exception:
        return None


def dt_ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return xs[lo]
    return xs[lo] * (hi - k) + xs[hi] * (k - lo)


def parse_labels(block: str | None) -> dict[str, str]:
    if not block:
        return {}
    out = {}
    for k, v in LABEL_RE.findall(block):
        out[k] = bytes(v, "utf-8").decode("unicode_escape")
    return out


def parse_metrics(text: str) -> list[tuple[str, dict[str, str], float]]:
    out = []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = METRIC_RE.match(line)
        if not m:
            continue
        name, block, raw = m.groups()
        try:
            val = float(raw)
        except ValueError:
            continue
        if math.isfinite(val):
            out.append((name, parse_labels(block), val))
    return out


def metric_sum(rows, name: str, labels: dict[str, str] | None = None) -> float:
    labels = labels or {}
    total = 0.0
    for n, ls, v in rows:
        if n != name:
            continue
        if all(ls.get(k) == x for k, x in labels.items()):
            total += v
    return total


def metric_max(rows, name: str, labels: dict[str, str] | None = None) -> float | None:
    labels = labels or {}
    vals = [v for n, ls, v in rows if n == name and all(ls.get(k) == x for k, x in labels.items())]
    return max(vals) if vals else None


def open_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS snapshots (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts TEXT NOT NULL,
          mcp TEXT NOT NULL,
          healthy INTEGER NOT NULL,
          ready INTEGER NOT NULL,
          tool_calls_total REAL,
          tool_calls_sum_ms REAL,
          poll_errors_total REAL,
          queue_length REAL,
          queue_capacity REAL,
          worker_occupancy REAL,
          worker_capacity REAL,
          process_cpu_seconds REAL,
          rss_bytes REAL,
          net_rx_bytes REAL,
          net_tx_bytes REAL,
          uptime_seconds REAL,
          status_json TEXT,
          metrics_json TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_snapshots_mcp_ts ON snapshots(mcp, ts);
        CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON snapshots(ts);

        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          mcp TEXT NOT NULL,
          client_instance_id TEXT NOT NULL DEFAULT '',
          seq INTEGER NOT NULL,
          ts TEXT NOT NULL,
          level TEXT,
          message TEXT,
          request_id TEXT,
          cmd_request_id TEXT,
          session_id TEXT,
          rpc_method TEXT,
          status_code INTEGER,
          has_error INTEGER,
          attrs_json TEXT,
          UNIQUE(mcp, client_instance_id, seq)
        );
        CREATE INDEX IF NOT EXISTS idx_events_mcp_ts ON events(mcp, ts);
        CREATE INDEX IF NOT EXISTS idx_events_request ON events(mcp, request_id);

        CREATE TABLE IF NOT EXISTS requests (
          request_key TEXT PRIMARY KEY,
          mcp TEXT NOT NULL,
          request_id TEXT NOT NULL,
          client_instance_id TEXT,
          cmd_request_id TEXT,
          session_id TEXT,
          rpc_method TEXT,
          input_at TEXT,
          mcp_reply_at TEXT,
          delivered_at TEXT,
          duration_ms REAL,
          reply_to_delivery_ms REAL,
          status_code INTEGER,
          has_error INTEGER,
          timing_confidence TEXT,
          observed_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_requests_mcp_delivered ON requests(mcp, delivered_at);
        CREATE INDEX IF NOT EXISTS idx_requests_duration ON requests(duration_ms);

        CREATE TABLE IF NOT EXISTS collector_state (
          mcp TEXT PRIMARY KEY,
          last_tool_count REAL,
          last_tool_sum_ms REAL,
          last_client_instance_id TEXT,
          last_collect_ts TEXT
        );
        """
    )
    c.commit()
    return c


DB = open_db()
ensure_schema(DB)
capture_proxy = CaptureProxyManager(DB)


class Collector:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=3.0)
        self.pending_metric_batches: dict[str, deque] = defaultdict(deque)
        self.pending_delivered: dict[str, deque] = defaultdict(deque)
        self.last_debug_set: dict[str, float] = defaultdict(float)
        self.latest_raw_metrics: dict[str, str] = {}
        self.latest_status: dict[str, dict[str, Any]] = {}
        self.latest_error: dict[str, str] = {}
        self.last_cleanup = 0.0

    async def close(self):
        await self.client.aclose()

    async def set_debug(self, slug: str, base: str):
        if not ENABLE_DEBUG or time.time() - self.last_debug_set[slug] < 60:
            return
        try:
            r = await self.client.put(f"{base}/api/log-level", json={"level": "debug"})
            r.raise_for_status()
            self.last_debug_set[slug] = time.time()
        except Exception as e:
            self.latest_error[slug] = f"debug level: {e}"

    async def get_text(self, url: str) -> tuple[bool, str]:
        try:
            r = await self.client.get(url)
            return r.status_code < 400, r.text
        except Exception as e:
            return False, str(e)

    async def collect_one(self, slug: str, cfg: dict[str, Any]):
        base = cfg["base"]
        try:
            await self.set_debug(slug, base)
            health_t, ready_t, status_r, metrics_r, logs_r = await asyncio.gather(
                self.get_text(f"{base}/healthz"),
                self.get_text(f"{base}/readyz"),
                self.client.get(f"{base}/api/status"),
                self.client.get(f"{base}/metrics"),
                self.client.get(f"{base}/api/logs?limit=1000"),
                return_exceptions=True,
            )

            healthy = int(isinstance(health_t, tuple) and health_t[0] and health_t[1].strip() == "live")
            ready = int(isinstance(ready_t, tuple) and ready_t[0] and ready_t[1].strip() == "ready")
            TUNNEL_HEALTH.labels(slug, "health").set(healthy)
            TUNNEL_HEALTH.labels(slug, "ready").set(ready)

            if isinstance(status_r, Exception) or status_r.status_code >= 400:
                status = {}
            else:
                status = status_r.json()
            if isinstance(metrics_r, Exception) or metrics_r.status_code >= 400:
                raise RuntimeError(f"metrics unavailable: {metrics_r}")
            metrics_text = metrics_r.text
            self.latest_raw_metrics[slug] = metrics_text
            self.latest_status[slug] = status
            rows = parse_metrics(metrics_text)

            count = metric_sum(rows, "command_end_to_end_latency_milliseconds_count", {
                "latency_type": "enqueue_to_response", "request_method": "tools/call"
            })
            sum_ms = metric_sum(rows, "command_end_to_end_latency_milliseconds_sum", {
                "latency_type": "enqueue_to_response", "request_method": "tools/call"
            })

            state = DB.execute("SELECT * FROM collector_state WHERE mcp=?", (slug,)).fetchone()
            previous_count = state["last_tool_count"] if state else None
            previous_sum = state["last_tool_sum_ms"] if state else None
            baseline = previous_count is None

            if previous_count is not None and count >= previous_count:
                dc = int(round(count - previous_count))
                ds = max(0.0, sum_ms - previous_sum)
                if dc > 0:
                    self.pending_metric_batches[slug].append({"count": dc, "sum_ms": ds, "ts": now_iso()})

            new_delivered = []
            if not isinstance(logs_r, Exception) and logs_r.status_code < 400:
                payload = logs_r.json()
                for ev in payload.get("events", []):
                    attrs = ev.get("attrs") or {}
                    client_id = str(attrs.get("client_instance_id") or status.get("client_instance_id") or "")
                    seq = int(ev.get("seq") or 0)
                    ts = parse_dt(ev.get("time")) or now_iso()
                    message = str(ev.get("message") or "")
                    request_id = str(attrs.get("request_id") or "")
                    # Raw HTTP debug events can contain credentials and must never be persisted.
                    if message.lower().startswith("raw http"):
                        continue
                    safe_attrs = {k: v for k, v in attrs.items() if k != "dump"}
                    try:
                        cur = DB.execute(
                            """INSERT OR IGNORE INTO events
                            (mcp,client_instance_id,seq,ts,level,message,request_id,cmd_request_id,session_id,rpc_method,status_code,has_error,attrs_json)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                slug, client_id, seq, ts, ev.get("level"), message, request_id,
                                attrs.get("cmd_request_id"), attrs.get("session_id"), attrs.get("rpc_method"),
                                attrs.get("status_code"), None if attrs.get("has_error") is None else int(bool(attrs.get("has_error"))),
                                json.dumps(safe_attrs, separators=(",", ":")),
                            ),
                        )
                        inserted = cur.rowcount > 0
                    except sqlite3.Error:
                        inserted = False

                    if request_id and message == "dispatcher received response from MCP server":
                        key = f"{slug}:{request_id}"
                        DB.execute(
                            """INSERT INTO requests(request_key,mcp,request_id,client_instance_id,cmd_request_id,session_id,rpc_method,mcp_reply_at,has_error,observed_at)
                            VALUES(?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(request_key) DO UPDATE SET
                              client_instance_id=excluded.client_instance_id,
                              cmd_request_id=COALESCE(excluded.cmd_request_id,requests.cmd_request_id),
                              session_id=COALESCE(excluded.session_id,requests.session_id),
                              rpc_method=COALESCE(excluded.rpc_method,requests.rpc_method),
                              mcp_reply_at=excluded.mcp_reply_at,
                              has_error=COALESCE(excluded.has_error,requests.has_error),
                              observed_at=excluded.observed_at""",
                            (key, slug, request_id, client_id, attrs.get("cmd_request_id"), attrs.get("session_id"),
                             attrs.get("rpc_method"), ts,
                             None if attrs.get("has_error") is None else int(bool(attrs.get("has_error"))), now_iso()),
                        )

                    if request_id and message == "dispatcher delivered response to control plane":
                        key = f"{slug}:{request_id}"
                        DB.execute(
                            """INSERT INTO requests(request_key,mcp,request_id,client_instance_id,cmd_request_id,session_id,rpc_method,delivered_at,status_code,has_error,observed_at)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(request_key) DO UPDATE SET
                              client_instance_id=excluded.client_instance_id,
                              cmd_request_id=COALESCE(excluded.cmd_request_id,requests.cmd_request_id),
                              session_id=COALESCE(excluded.session_id,requests.session_id),
                              rpc_method=COALESCE(excluded.rpc_method,requests.rpc_method),
                              delivered_at=excluded.delivered_at,
                              status_code=COALESCE(excluded.status_code,requests.status_code),
                              has_error=COALESCE(excluded.has_error,requests.has_error),
                              observed_at=excluded.observed_at""",
                            (key, slug, request_id, client_id, attrs.get("cmd_request_id"), attrs.get("session_id"),
                             attrs.get("rpc_method"), ts, attrs.get("status_code"),
                             None if attrs.get("has_error") is None else int(bool(attrs.get("has_error"))), now_iso()),
                        )
                        if inserted and not baseline:
                            new_delivered.append(key)
                            self.pending_delivered[slug].append(key)
                            OBSERVED_REQUESTS.labels(slug, "error" if attrs.get("has_error") else "ok").inc()

            self.reconcile(slug)
            self.update_reply_delta(slug)

            metrics_small = {
                "tool_calls_total": count,
                "tool_calls_sum_ms": sum_ms,
                "poll_errors_total": metric_sum(rows, "commands_poll_errors_total"),
                "queue_length": metric_max(rows, "commands_queue_length"),
                "queue_capacity": metric_max(rows, "commands_queue_capacity"),
                "worker_occupancy": metric_max(rows, "dispatcher_worker_pool_occupancy"),
                "worker_capacity": metric_max(rows, "dispatcher_worker_pool_capacity"),
                "process_cpu_seconds": metric_max(rows, "process_cpu_seconds_total"),
                "rss_bytes": metric_max(rows, "process_resident_memory_bytes"),
                "net_rx_bytes": metric_max(rows, "process_network_receive_bytes_total"),
                "net_tx_bytes": metric_max(rows, "process_network_transmit_bytes_total"),
            }
            ts = now_iso()
            DB.execute(
                """INSERT INTO snapshots(ts,mcp,healthy,ready,tool_calls_total,tool_calls_sum_ms,poll_errors_total,queue_length,queue_capacity,
                   worker_occupancy,worker_capacity,process_cpu_seconds,rss_bytes,net_rx_bytes,net_tx_bytes,uptime_seconds,status_json,metrics_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    ts, slug, healthy, ready, metrics_small["tool_calls_total"], metrics_small["tool_calls_sum_ms"],
                    metrics_small["poll_errors_total"], metrics_small["queue_length"], metrics_small["queue_capacity"],
                    metrics_small["worker_occupancy"], metrics_small["worker_capacity"], metrics_small["process_cpu_seconds"],
                    metrics_small["rss_bytes"], metrics_small["net_rx_bytes"], metrics_small["net_tx_bytes"],
                    status.get("uptime_seconds"), json.dumps(status, separators=(",", ":")),
                    json.dumps(metrics_small, separators=(",", ":")),
                ),
            )
            DB.execute(
                """INSERT INTO collector_state(mcp,last_tool_count,last_tool_sum_ms,last_client_instance_id,last_collect_ts)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(mcp) DO UPDATE SET last_tool_count=excluded.last_tool_count,last_tool_sum_ms=excluded.last_tool_sum_ms,
                   last_client_instance_id=excluded.last_client_instance_id,last_collect_ts=excluded.last_collect_ts""",
                (slug, count, sum_ms, status.get("client_instance_id"), ts),
            )
            # Keep high-frequency snapshots for 48h; aggregate requests/events remain.
            if time.monotonic() - self.last_cleanup > 3600:
                cutoff = datetime.fromtimestamp(time.time() - 48 * 3600, timezone.utc).isoformat()
                DB.execute("DELETE FROM snapshots WHERE ts < ?", (cutoff,))
                self.last_cleanup = time.monotonic()
            DB.commit()
            LAST_COLLECT.labels(slug).set(time.time())
            self.latest_error.pop(slug, None)
        except Exception as e:
            COLLECT_ERRORS.labels(slug).inc()
            self.latest_error[slug] = str(e)

    def reconcile(self, slug: str):
        batches = self.pending_metric_batches[slug]
        pending = self.pending_delivered[slug]
        while batches and pending:
            batch = batches[0]
            n = int(batch["count"])
            if len(pending) < n:
                break
            keys = [pending.popleft() for _ in range(n)]
            batches.popleft()
            avg = batch["sum_ms"] / n if n else None
            confidence = "exact-single" if n == 1 else f"batch-average-{n}"
            for key in keys:
                row = DB.execute("SELECT delivered_at FROM requests WHERE request_key=?", (key,)).fetchone()
                if not row or not row["delivered_at"] or avg is None:
                    continue
                end = dt_ts(row["delivered_at"])
                input_at = datetime.fromtimestamp(end - avg / 1000.0, timezone.utc).isoformat() if end else None
                DB.execute(
                    "UPDATE requests SET duration_ms=?, input_at=?, timing_confidence=?, observed_at=? WHERE request_key=?",
                    (avg, input_at, confidence, now_iso(), key),
                )
                OBSERVED_LATENCY.labels(slug).observe(avg / 1000.0)

    def update_reply_delta(self, slug: str):
        rows = DB.execute(
            "SELECT request_key,mcp_reply_at,delivered_at FROM requests WHERE mcp=? AND mcp_reply_at IS NOT NULL AND delivered_at IS NOT NULL AND reply_to_delivery_ms IS NULL",
            (slug,),
        ).fetchall()
        for r in rows:
            a, b = dt_ts(r["mcp_reply_at"]), dt_ts(r["delivered_at"])
            if a is not None and b is not None:
                DB.execute("UPDATE requests SET reply_to_delivery_ms=? WHERE request_key=?", ((b - a) * 1000.0, r["request_key"]))

    async def loop(self):
        await asyncio.sleep(0.3)
        while True:
            await asyncio.gather(*(self.collect_one(k, v) for k, v in TUNNELS.items()))
            ingest_stdio_files(DB)
            link_captures(DB)
            await asyncio.sleep(POLL_SECONDS)


collector = Collector()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await capture_proxy.start()
    task = asyncio.create_task(collector.loop())
    yield
    task.cancel()
    try:
        await task
    except BaseException:
        pass
    await collector.close()
    await capture_proxy.close()
    DB.close()


app = FastAPI(title="MCP Observability Dashboard", version="1.0.0", lifespan=lifespan)


def where_range(from_: str | None, to: str | None, mcp: str | None, col: str = "delivered_at"):
    cond, args = [f"{col} IS NOT NULL"], []
    f, t = parse_dt(from_), parse_dt(to)
    if f:
        cond.append(f"{col} >= ?")
        args.append(f)
    if t:
        cond.append(f"{col} <= ?")
        args.append(t)
    if mcp and mcp != "all":
        cond.append("mcp = ?")
        args.append(mcp)
    return " AND ".join(cond), args


@app.get("/healthz")
async def healthz():
    recent_cutoff = datetime.fromtimestamp(time.time() - max(10, POLL_SECONDS * 6), timezone.utc).isoformat()
    row = DB.execute("SELECT COUNT(DISTINCT mcp) n FROM snapshots WHERE ts>=?", (recent_cutoff,)).fetchone()
    ok = (row["n"] or 0) >= max(1, len(TUNNELS) - 1)
    return {"status": "ok" if ok else "degraded", "collectors_seen": row["n"], "expected": len(TUNNELS), "errors": collector.latest_error}


@app.get("/metrics")
async def own_metrics():
    return PlainTextResponse(generate_latest().decode(), media_type=CONTENT_TYPE_LATEST)


@app.get("/api/config")
async def config():
    return {"poll_seconds": POLL_SECONDS, "debug_logs": ENABLE_DEBUG, "tunnels": TUNNELS, "http_capture": HTTP_CAPTURE}


@app.get("/api/summary")
async def summary(from_: str | None = Query(None, alias="from"), to: str | None = None, mcp: str | None = "all"):
    where, args = where_range(from_, to, mcp)
    reqs = DB.execute(f"SELECT duration_ms,has_error,status_code FROM requests WHERE {where}", args).fetchall()
    durs = [float(r["duration_ms"]) for r in reqs if r["duration_ms"] is not None]
    errs = sum(1 for r in reqs if r["has_error"] or (r["status_code"] and r["status_code"] >= 400))
    per_mcp = []
    for slug, cfg in TUNNELS.items():
        w, a = where_range(from_, to, slug)
        rs = DB.execute(f"SELECT duration_ms,has_error,status_code FROM requests WHERE {w}", a).fetchall()
        ds = [float(x["duration_ms"]) for x in rs if x["duration_ms"] is not None]
        per_mcp.append({
            "mcp": slug, "name": cfg["name"], "count": len(rs),
            "avg_ms": statistics.fmean(ds) if ds else None,
            "p95_ms": percentile(ds, .95),
            "max_ms": max(ds) if ds else None,
            "errors": sum(1 for x in rs if x["has_error"] or (x["status_code"] and x["status_code"] >= 400)),
        })
    method_rows = DB.execute(
        f"SELECT COALESCE(rpc_method,'unknown') method, COUNT(*) count, AVG(duration_ms) avg_ms, MAX(duration_ms) max_ms FROM requests WHERE {where} GROUP BY 1 ORDER BY count DESC",
        args,
    ).fetchall()
    return {
        "total_requests": len(reqs), "errors": errs,
        "success_rate": ((len(reqs) - errs) / len(reqs) * 100.0) if reqs else None,
        "avg_ms": statistics.fmean(durs) if durs else None,
        "p50_ms": percentile(durs, .50), "p95_ms": percentile(durs, .95), "p99_ms": percentile(durs, .99),
        "max_ms": max(durs) if durs else None,
        "per_mcp": per_mcp, "methods": [dict(x) for x in method_rows],
    }


@app.get("/api/requests")
async def requests_api(
    from_: str | None = Query(None, alias="from"), to: str | None = None, mcp: str | None = "all",
    sort: str = "newest", limit: int = Query(250, ge=1, le=5000), errors_only: bool = False
):
    where, args = where_range(from_, to, mcp)
    if errors_only:
        where += " AND (has_error=1 OR status_code>=400)"
    orders = {
        "newest": "delivered_at DESC", "oldest": "delivered_at ASC",
        "slowest": "duration_ms DESC NULLS LAST", "fastest": "duration_ms ASC NULLS LAST",
        "reply_delay": "reply_to_delivery_ms DESC NULLS LAST",
    }
    order = orders.get(sort, orders["newest"])
    public_cols = """
      request_key,mcp,request_id,client_instance_id,cmd_request_id,session_id,rpc_method,
      input_at,mcp_reply_at,delivered_at,duration_ms,reply_to_delivery_ms,status_code,has_error,
      timing_confidence,observed_at,tool_name,capture_source,payload_captured_at,
      CASE WHEN input_json IS NOT NULL AND output_json IS NOT NULL THEN 1 ELSE 0 END AS payload_available
    """
    rows = DB.execute(f"SELECT {public_cols} FROM requests WHERE {where} ORDER BY {order} LIMIT ?", [*args, limit]).fetchall()
    return {"rows": [dict(r) for r in rows]}


@app.get("/api/request/{mcp}/{request_id}")
async def request_detail(mcp: str, request_id: str):
    row = DB.execute("SELECT * FROM requests WHERE mcp=? AND request_id=? LIMIT 1", (mcp, request_id)).fetchone()
    if row is None:
        raise HTTPException(404, "request not found")
    out = dict(row)
    for field in ("input_json", "output_json"):
        raw = out.get(field)
        if raw:
            try:
                out[field] = json.loads(raw)
            except Exception:
                out[field] = raw
    return out


@app.get("/api/timeseries")
async def timeseries(from_: str | None = Query(None, alias="from"), to: str | None = None, mcp: str | None = "all"):
    where, args = where_range(from_, to, mcp)
    rows = DB.execute(f"SELECT delivered_at,duration_ms,has_error,status_code FROM requests WHERE {where} ORDER BY delivered_at", args).fetchall()
    if not rows:
        return {"bucket_seconds": 60, "points": []}
    start = dt_ts(rows[0]["delivered_at"]) or time.time()
    end = dt_ts(rows[-1]["delivered_at"]) or start
    span = max(1, end - start)
    target = span / 120
    choices = [1, 5, 10, 30, 60, 300, 900, 3600, 21600, 86400]
    bucket = next((x for x in choices if x >= target), choices[-1])
    bins: dict[int, dict[str, Any]] = {}
    for r in rows:
        ts = dt_ts(r["delivered_at"])
        if ts is None:
            continue
        b = int(ts // bucket) * bucket
        x = bins.setdefault(b, {"count": 0, "errors": 0, "durations": []})
        x["count"] += 1
        if r["has_error"] or (r["status_code"] and r["status_code"] >= 400):
            x["errors"] += 1
        if r["duration_ms"] is not None:
            x["durations"].append(float(r["duration_ms"]))
    points = []
    for b in sorted(bins):
        x = bins[b]
        points.append({
            "ts": datetime.fromtimestamp(b, timezone.utc).isoformat(),
            "count": x["count"], "errors": x["errors"],
            "avg_ms": statistics.fmean(x["durations"]) if x["durations"] else None,
            "p95_ms": percentile(x["durations"], .95),
        })
    return {"bucket_seconds": bucket, "points": points}


@app.get("/api/health")
async def health_api():
    out = []
    for slug, cfg in TUNNELS.items():
        r = DB.execute("SELECT * FROM snapshots WHERE mcp=? ORDER BY ts DESC LIMIT 1", (slug,)).fetchone()
        status = collector.latest_status.get(slug, {})
        item = {
            "mcp": slug, "name": cfg["name"], "port": cfg["port"],
            "healthy": bool(r["healthy"]) if r else False,
            "ready": bool(r["ready"]) if r else False,
            "last_seen": r["ts"] if r else None,
            "error": collector.latest_error.get(slug),
            "tunnel_id": status.get("control_plane_tunnel_id"),
            "mcp_target": status.get("mcp_server_url") or (status.get("channels") or [{}])[0].get("details"),
            "started_at": status.get("started_at"),
            "uptime_seconds": status.get("uptime_seconds"),
        }
        if r:
            for k in ["tool_calls_total","poll_errors_total","queue_length","queue_capacity","worker_occupancy","worker_capacity",
                      "process_cpu_seconds","rss_bytes","net_rx_bytes","net_tx_bytes"]:
                item[k] = r[k]
        out.append(item)
    return {"rows": out}


@app.get("/api/telemetry")
async def telemetry(
    from_: str | None = Query(None, alias="from"), to: str | None = None, mcp: str | None = "all",
    level: str | None = "all", q: str | None = None, order: str = "newest",
    limit: int = Query(500, ge=1, le=5000)
):
    cond, args = ["1=1"], []
    f, t = parse_dt(from_), parse_dt(to)
    if f: cond.append("ts>=?"); args.append(f)
    if t: cond.append("ts<=?"); args.append(t)
    if mcp and mcp != "all": cond.append("mcp=?"); args.append(mcp)
    if level and level != "all": cond.append("lower(level)=?"); args.append(level.lower())
    if q:
        cond.append("(message LIKE ? OR attrs_json LIKE ? OR request_id LIKE ?)")
        x = f"%{q}%"; args.extend([x, x, x])
    direction = "ASC" if order == "oldest" else "DESC"
    rows = DB.execute(
        f"SELECT * FROM events WHERE {' AND '.join(cond)} ORDER BY ts {direction} LIMIT ?",
        [*args, limit]
    ).fetchall()
    return {"rows": [dict(r) for r in rows]}


@app.get("/api/snapshots")
async def snapshots(from_: str | None = Query(None, alias="from"), to: str | None = None, mcp: str | None = "all", limit: int = Query(1000, le=5000)):
    cond, args = ["1=1"], []
    f, t = parse_dt(from_), parse_dt(to)
    if f: cond.append("ts>=?"); args.append(f)
    if t: cond.append("ts<=?"); args.append(t)
    if mcp and mcp != "all": cond.append("mcp=?"); args.append(mcp)
    rows = DB.execute(
        f"SELECT ts,mcp,healthy,ready,tool_calls_total,poll_errors_total,queue_length,queue_capacity,worker_occupancy,worker_capacity,rss_bytes,net_rx_bytes,net_tx_bytes FROM snapshots WHERE {' AND '.join(cond)} ORDER BY ts DESC LIMIT ?",
        [*args, limit]
    ).fetchall()
    return {"rows": [dict(r) for r in rows]}


@app.get("/api/raw/metrics/{mcp}")
async def raw_metrics(mcp: str):
    if mcp not in TUNNELS:
        raise HTTPException(404)
    return PlainTextResponse(collector.latest_raw_metrics.get(mcp, "No sample yet\n"))


@app.get("/api/raw/status/{mcp}")
async def raw_status(mcp: str):
    if mcp not in TUNNELS:
        raise HTTPException(404)
    return collector.latest_status.get(mcp, {})


app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    return (Path(__file__).parent / "static" / "index.html").read_text()


# Grafana is intentionally isolated from the MCP transport and old production app.
# Reverse proxying keeps it available at /grafana/ behind the existing dashboard origin.
GRAFANA_UPSTREAM = os.getenv("MCP_OBS_GRAFANA", "http://127.0.0.1:3120").rstrip("/")

@app.api_route("/grafana/{tail:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def grafana_proxy(request: Request, tail: str):
    exclude = {"host", "connection", "content-length", "transfer-encoding", "content-encoding"}
    headers = {k:v for k,v in request.headers.items() if k.lower() not in exclude}
    # Redirects and links should refer to the same /grafana/ prefix.
    headers["x-forwarded-prefix"] = "/grafana"
    headers["x-forwarded-proto"] = request.headers.get("x-forwarded-proto", request.url.scheme)
    query = f"?{request.url.query}" if request.url.query else ""
    try:
        async with httpx.AsyncClient(timeout=40.0,follow_redirects=False) as client:
            up = await client.request(request.method,f"{GRAFANA_UPSTREAM}/grafana/{tail}{query}", headers=headers, content=await request.body())
        response_headers={k:v for k,v in up.headers.items() if k.lower() not in exclude and k.lower() not in {"set-cookie","content-security-policy"}}
        return Response(content=up.content,status_code=up.status_code,headers=response_headers)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Grafana not available: {type(e).__name__}") from e

@app.get("/grafana")
async def grafana_redirect():
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/grafana/")
