from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiohttp import ClientSession, ClientTimeout, web

CAPTURE_DIR = Path(os.getenv("MCP_OBS_CAPTURE_DIR", "/data/captures"))
MAX_CAPTURE_BYTES = int(os.getenv("MCP_OBS_MAX_CAPTURE_BYTES", str(5 * 1024 * 1024)))

DEFAULT_HTTP_CAPTURE = {
    "terminal": {"listen_port": 18900, "target": "http://127.0.0.1:8900"},
    "excel": {"listen_port": 18017, "target": "http://127.0.0.1:8017"},
    "personal": {"listen_port": 18765, "target": "http://127.0.0.1:8765"},
    "background": {"listen_port": 17874, "target": "http://127.0.0.1:17873"},
}
HTTP_CAPTURE = json.loads(os.getenv("MCP_OBS_HTTP_CAPTURE_JSON", "null") or "null") or DEFAULT_HTTP_CAPTURE

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length", "host",
}

SECRET_KEY_RE = re.compile(
    r"(?:password|passwd|pwd|token|secret|authorization|cookie|api[_-]?key|"
    r"private[_-]?key|client[_-]?secret|access[_-]?key|refresh[_-]?token)",
    re.I,
)
SECRET_NAME = r"(?:password|passwd|pwd|token|secret|api[_-]?key|client[_-]?secret|access[_-]?key)"
QUOTED_INLINE_SECRET_RE = re.compile(
    rf"(?i)\b({SECRET_NAME})(\s*[:=]\s*)([\"'])(.*?)\3"
)
INLINE_SECRET_RE = re.compile(
    rf"(?i)\b({SECRET_NAME})(\s*[:=]\s*)([^\s;\"']+)"
)
AUTH_HEADER_RE = re.compile(
    r"(?i)(Authorization\s*:\s*(?:Bearer|Basic)\s+)([\"']?)([^\s\"']+)([\"']?)"
)
OPENAI_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")
GITHUB_TOKEN_RE = re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")
AWS_KEY_RE = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.S,
)


def redact_text(value: str) -> str:
    value = AUTH_HEADER_RE.sub(lambda m: m.group(1) + m.group(2) + "[REDACTED]" + m.group(4), value)
    value = QUOTED_INLINE_SECRET_RE.sub(
        lambda m: m.group(1) + m.group(2) + m.group(3) + "[REDACTED]" + m.group(3), value
    )
    value = INLINE_SECRET_RE.sub(lambda m: m.group(1) + m.group(2) + "[REDACTED]", value)
    value = OPENAI_KEY_RE.sub("[REDACTED_OPENAI_KEY]", value)
    value = GITHUB_TOKEN_RE.sub("[REDACTED_GITHUB_TOKEN]", value)
    value = AWS_KEY_RE.sub("[REDACTED_AWS_KEY]", value)
    value = PRIVATE_KEY_RE.sub("[REDACTED_PRIVATE_KEY]", value)
    return value


def redact_value(value: Any, key: str | None = None) -> Any:
    if key and SECRET_KEY_RE.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {k: redact_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_schema(db: sqlite3.Connection) -> None:
    cols = {r[1] for r in db.execute("PRAGMA table_info(requests)").fetchall()}
    additions = {
        "tool_name": "TEXT",
        "input_json": "TEXT",
        "output_json": "TEXT",
        "capture_source": "TEXT",
        "payload_captured_at": "TEXT",
    }
    for name, typ in additions.items():
        if name not in cols:
            db.execute(f"ALTER TABLE requests ADD COLUMN {name} {typ}")

    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS captures (
          capture_key TEXT PRIMARY KEY,
          mcp TEXT NOT NULL,
          source TEXT NOT NULL,
          cmd_request_id TEXT,
          rpc_id TEXT,
          rpc_method TEXT,
          tool_name TEXT,
          request_at TEXT,
          response_at TEXT,
          request_json TEXT,
          response_json TEXT,
          linked_request_key TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_captures_cmd ON captures(mcp, cmd_request_id);
        CREATE INDEX IF NOT EXISTS idx_captures_link ON captures(linked_request_key);
        CREATE TABLE IF NOT EXISTS capture_offsets (
          path TEXT PRIMARY KEY,
          byte_offset INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    db.commit()


def _bounded_json(obj: Any) -> str:
    obj = redact_value(obj)
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    raw = text.encode("utf-8")
    if len(raw) <= MAX_CAPTURE_BYTES:
        return text
    preview = raw[:MAX_CAPTURE_BYTES].decode("utf-8", errors="replace")
    return json.dumps({
        "_capture_truncated": True,
        "_original_bytes_at_least": len(raw),
        "_preview": preview,
    }, ensure_ascii=False, separators=(",", ":"))


def sanitize_request(obj: Any) -> tuple[str | None, str | None, str | None]:
    if not isinstance(obj, dict):
        return None, None, None
    method = obj.get("method")
    params = obj.get("params")
    if method != "tools/call" or not isinstance(params, dict):
        return str(method) if method else None, None, None
    tool_name = params.get("name")
    safe = {
        "jsonrpc": obj.get("jsonrpc", "2.0"),
        "id": obj.get("id"),
        "method": method,
        "params": {
            "name": tool_name,
            "arguments": params.get("arguments"),
        },
    }
    return method, str(tool_name) if tool_name else None, _bounded_json(safe)


def sanitize_response(obj: Any) -> str | None:
    if not isinstance(obj, dict):
        return None
    safe = {"jsonrpc": obj.get("jsonrpc", "2.0"), "id": obj.get("id")}
    if "result" in obj:
        safe["result"] = obj.get("result")
    if "error" in obj:
        safe["error"] = obj.get("error")
    return _bounded_json(safe)


def _json_body_from_sse(body: bytes) -> Any:
    text = body.decode("utf-8", errors="replace")
    for line in text.splitlines():
        if line.startswith("data:"):
            payload = line[5:].strip()
            try:
                return json.loads(payload)
            except Exception:
                continue
    try:
        return json.loads(text)
    except Exception:
        return None


class CaptureProxyManager:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.session: ClientSession | None = None
        self.runners: list[web.AppRunner] = []

    async def start(self) -> None:
        self.session = ClientSession(timeout=ClientTimeout(total=None, connect=5, sock_connect=5))
        for slug, cfg in HTTP_CAPTURE.items():
            app = web.Application(client_max_size=max(MAX_CAPTURE_BYTES * 2, 16 * 1024 * 1024))
            app["slug"] = slug
            app["target"] = cfg["target"].rstrip("/")
            app.router.add_route("*", "/{tail:.*}", self._handle)
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", int(cfg["listen_port"]))
            await site.start()
            self.runners.append(runner)

    async def close(self) -> None:
        for r in self.runners:
            await r.cleanup()
        self.runners.clear()
        if self.session:
            await self.session.close()
            self.session = None

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        assert self.session is not None
        slug = request.app["slug"]
        target = request.app["target"]
        request_at = iso_now()
        body = await request.read()
        cmd_request_id = request.headers.get("X-Request-Id")
        rpc_id = None
        rpc_method = None
        tool_name = None
        request_json = None
        try:
            req_obj = json.loads(body) if body else None
            if isinstance(req_obj, dict):
                rpc_id = str(req_obj.get("id")) if req_obj.get("id") is not None else None
                rpc_method, tool_name, request_json = sanitize_request(req_obj)
        except Exception:
            req_obj = None

        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
        url = target + request.rel_url.path_qs
        async with self.session.request(
            request.method, url, headers=headers, data=body, allow_redirects=False
        ) as upstream:
            out_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in HOP_BY_HOP}
            response = web.StreamResponse(status=upstream.status, reason=upstream.reason, headers=out_headers)
            await response.prepare(request)
            captured = bytearray()
            async for chunk in upstream.content.iter_chunked(65536):
                if len(captured) < MAX_CAPTURE_BYTES:
                    captured.extend(chunk[: MAX_CAPTURE_BYTES - len(captured)])
                await response.write(chunk)
            await response.write_eof()
            response_at = iso_now()

        if rpc_method == "tools/call" and request_json:
            resp_obj = _json_body_from_sse(bytes(captured))
            response_json = sanitize_response(resp_obj)
            key = f"{slug}:http:{cmd_request_id or ''}:{rpc_id or ''}:{request_at}"
            self.db.execute(
                """
                INSERT OR REPLACE INTO captures
                (capture_key,mcp,source,cmd_request_id,rpc_id,rpc_method,tool_name,request_at,response_at,request_json,response_json,linked_request_key)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,COALESCE((SELECT linked_request_key FROM captures WHERE capture_key=?),NULL))
                """,
                (key, slug, "http-proxy", cmd_request_id, rpc_id, rpc_method, tool_name,
                 request_at, response_at, request_json, response_json, key),
            )
            self.db.commit()
        return response


def ingest_stdio_files(db: sqlite3.Connection) -> int:
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    ingested = 0
    for path in sorted(CAPTURE_DIR.glob("*.jsonl")):
        row = db.execute("SELECT byte_offset FROM capture_offsets WHERE path=?", (str(path),)).fetchone()
        offset = int(row[0]) if row else 0
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            continue
        if size < offset:
            offset = 0
        with path.open("r", encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            while True:
                line = f.readline()
                if not line:
                    break
                offset = f.tell()
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("kind") == "request":
                    db.execute(
                        """
                        INSERT INTO captures(capture_key,mcp,source,rpc_id,rpc_method,tool_name,request_at,request_json)
                        VALUES(?,?,?,?,?,?,?,?)
                        ON CONFLICT(capture_key) DO UPDATE SET
                          rpc_id=excluded.rpc_id,rpc_method=excluded.rpc_method,tool_name=excluded.tool_name,
                          request_at=excluded.request_at,request_json=excluded.request_json
                        """,
                        (rec["interaction_id"], rec["mcp"], "stdio-wrapper", str(rec.get("rpc_id")),
                         rec.get("rpc_method"), rec.get("tool_name"), rec.get("ts"), rec.get("payload")),
                    )
                    ingested += 1
                elif rec.get("kind") == "response":
                    db.execute(
                        """
                        INSERT INTO captures(capture_key,mcp,source,rpc_id,response_at,response_json)
                        VALUES(?,?,?,?,?,?)
                        ON CONFLICT(capture_key) DO UPDATE SET
                          response_at=excluded.response_at,response_json=excluded.response_json
                        """,
                        (rec["interaction_id"], rec["mcp"], "stdio-wrapper", str(rec.get("rpc_id")),
                         rec.get("ts"), rec.get("payload")),
                    )
                    ingested += 1
        db.execute(
            "INSERT INTO capture_offsets(path,byte_offset) VALUES(?,?) ON CONFLICT(path) DO UPDATE SET byte_offset=excluded.byte_offset",
            (str(path), offset),
        )
    if ingested:
        db.commit()
    return ingested


def link_captures(db: sqlite3.Connection) -> int:
    linked = 0
    rows = db.execute(
        "SELECT * FROM captures WHERE linked_request_key IS NULL AND request_json IS NOT NULL AND response_json IS NOT NULL ORDER BY request_at"
    ).fetchall()
    for c in rows:
        req = None
        if c["cmd_request_id"]:
            req = db.execute(
                "SELECT * FROM requests WHERE mcp=? AND cmd_request_id=? ORDER BY delivered_at DESC LIMIT 1",
                (c["mcp"], c["cmd_request_id"]),
            ).fetchone()
        if req is None and c["response_at"]:
            candidates = db.execute(
                """
                SELECT * FROM requests
                WHERE mcp=? AND rpc_method='tools/call' AND output_json IS NULL AND mcp_reply_at IS NOT NULL
                ORDER BY delivered_at DESC LIMIT 100
                """,
                (c["mcp"],),
            ).fetchall()
            target = datetime.fromisoformat(c["response_at"].replace("Z", "+00:00")).timestamp()
            best = None
            best_delta = 999999.0
            for x in candidates:
                try:
                    ts = datetime.fromisoformat(x["mcp_reply_at"].replace("Z", "+00:00")).timestamp()
                except Exception:
                    continue
                d = abs(ts - target)
                if d < best_delta:
                    best, best_delta = x, d
            if best is not None and best_delta <= 5.0:
                req = best
        if req is None:
            continue
        db.execute(
            """
            UPDATE requests SET
              tool_name=?, input_json=?, output_json=?, capture_source=?, payload_captured_at=?
            WHERE request_key=?
            """,
            (c["tool_name"], c["request_json"], c["response_json"], c["source"], iso_now(), req["request_key"]),
        )
        db.execute("UPDATE captures SET linked_request_key=? WHERE capture_key=?", (req["request_key"], c["capture_key"]))
        linked += 1
    if linked:
        db.commit()
    return linked
