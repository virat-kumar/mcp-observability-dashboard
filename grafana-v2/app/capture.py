from __future__ import annotations

import asyncio
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
_configured_capture = os.getenv("MCP_OBS_HTTP_CAPTURE_JSON")
HTTP_CAPTURE = DEFAULT_HTTP_CAPTURE if not _configured_capture else json.loads(_configured_capture)

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


# The original Terminal MCP process serves its ChatGPT app from a compiled Go
# constant. An optional file-backed stylesheet adjusts only that *view* without
# restarting the user's long-lived terminal sessions or changing MCP tool data.
_TERMINAL_MONITOR_RESOURCES = frozenset(
    f"ui://terminal-mcp/monitor-v{version}.html" for version in (1, 2, 3)
)
_TERMINAL_MONITOR_V4 = "ui://terminal-mcp/monitor-v4.html"
_TERMINAL_MONITOR_V2 = "ui://terminal-mcp/monitor-v2.html"
_TERMINAL_MONITOR_STYLE_MARKER = "/* terminal-monitor-viewport-overlay */"
_MAX_TERMINAL_MONITOR_RESOURCE_BYTES = 256 * 1024


def _terminal_monitor_stylesheet() -> str | None:
    path = os.getenv("MCP_OBS_TERMINAL_MONITOR_CSS_FILE", "")
    if not path:
        return None
    try:
        file = Path(path)
        if file.is_symlink() or not file.is_file():
            return None
        raw = file.read_bytes()
        if not 0 < len(raw) <= 8192:
            return None
        css = raw.decode("utf-8").strip()
        # This locally managed file may contain CSS only, not HTML/script or
        # remote CSS imports. Never use a request-provided path or URL.
        if any(fragment in css.lower() for fragment in ("<", ">", "@import", "url(", "expression(")):
            return None
        return css
    except (OSError, UnicodeError):
        return None


def _overlay_terminal_resource_message(message: Any, uri: str, css: str) -> bool:
    if not isinstance(message, dict):
        return False
    result = message.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("contents"), list):
        return False
    changed = False
    for item in result["contents"]:
        if not isinstance(item, dict) or item.get("uri") != uri:
            continue
        if "text/html" not in str(item.get("mimeType", "")):
            continue
        doc = item.get("text")
        if not isinstance(doc, str) or "Ubuntu Terminal Monitor" not in doc or "</style>" not in doc:
            continue
        if _TERMINAL_MONITOR_STYLE_MARKER in doc or "height:100dvh" in doc:
            continue
        item["text"] = doc.replace(
            "</style>", "\n" + _TERMINAL_MONITOR_STYLE_MARKER + "\n" + css + "\n</style>", 1
        )
        changed = True
    return changed


def _remap_terminal_monitor_v4_response(body: bytes, content_type: str, method: str) -> tuple[bytes, bool]:
    """Advertise a new app URI to break the host's cached v2/v3 HTML.

    Never alter tool implementations, names, schema, capabilities or unrelated
    resources. The proxy uses the original v2 HTML for its virtual v4 resource.
    """
    if "application/json" not in content_type.lower():
        return body, False
    try:
        message = json.loads(body)
        result = message.get("result")
        if not isinstance(result, dict):
            return body, False
        changed = False
        if method == "tools/list":
            for tool in result.get("tools", []):
                if not isinstance(tool, dict) or tool.get("name") not in ("terminal_open", "terminal_monitor"):
                    continue
                meta = tool.get("_meta")
                if not isinstance(meta, dict):
                    continue
                if meta.get("openai/outputTemplate") in _TERMINAL_MONITOR_RESOURCES:
                    meta["openai/outputTemplate"] = _TERMINAL_MONITOR_V4
                    changed = True
                if isinstance(meta.get("ui"), dict) and meta["ui"].get("resourceUri") in _TERMINAL_MONITOR_RESOURCES:
                    meta["ui"]["resourceUri"] = _TERMINAL_MONITOR_V4
                    changed = True
        elif method == "resources/list":
            resources = result.get("resources")
            if isinstance(resources, list) and not any(isinstance(r,dict) and r.get("uri")==_TERMINAL_MONITOR_V4 for r in resources):
                source = next((r for r in resources if isinstance(r,dict) and r.get("uri")==_TERMINAL_MONITOR_V2), None)
                if source is not None:
                    resources.append({**source, "uri": _TERMINAL_MONITOR_V4})
                    changed = True
        elif method == "resources/read":
            for entry in result.get("contents", []):
                if isinstance(entry, dict) and entry.get("uri") == _TERMINAL_MONITOR_V2:
                    entry["uri"] = _TERMINAL_MONITOR_V4
                    changed = True
        if changed:
            return json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode(), True
    except (UnicodeError, ValueError, TypeError):
        pass
    return body, False


def _overlay_terminal_resource_response(body: bytes, content_type: str, uri: str, css: str) -> tuple[bytes, bool]:
    """Change only the selected MCP app's HTML; leave other response bytes alone.

    MCP Streamable HTTP may return a regular JSON response or a single/multiple
    SSE events, each containing one JSON-RPC response. Preserve SSE framing.
    """
    if uri not in _TERMINAL_MONITOR_RESOURCES:
        return body, False
    if "text/event-stream" in content_type.lower():
        try:
            text = body.decode("utf-8")
            # Keep event separators, fields and unmodified events byte-exact.
            chunks = re.split(r"(\r?\n\r?\n)", text)
            changed = False
            for index in range(0, len(chunks), 2):
                lines = chunks[index].splitlines(keepends=True)
                data_lines = [j for j, line in enumerate(lines) if line.startswith("data:")]
                if not data_lines:
                    continue
                data = "\n".join(lines[j][5:].strip() for j in data_lines)
                try:
                    message = json.loads(data)
                except (ValueError, TypeError):
                    continue
                if not _overlay_terminal_resource_message(message, uri, css):
                    continue
                first = data_lines[0]
                end = "\r\n" if lines[first].endswith("\r\n") else "\n" if lines[first].endswith("\n") else ""
                lines[first] = "data: " + json.dumps(message, separators=(",", ":"), ensure_ascii=False) + end
                for j in reversed(data_lines[1:]):
                    del lines[j]
                chunks[index] = "".join(lines)
                changed = True
            return ("".join(chunks).encode("utf-8"), True) if changed else (body, False)
        except (UnicodeError, ValueError):
            return body, False
    if "application/json" not in content_type.lower():
        return body, False
    try:
        message = json.loads(body)
        if _overlay_terminal_resource_message(message, uri, css):
            return json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8"), True
    except (UnicodeError, ValueError, TypeError):
        pass
    return body, False


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
        # v4 is a virtual UI resource, served using the original v2 document.
        # Only this exact request gets a substituted upstream URI.
        v4_request = (slug == "terminal" and request.method == "POST"
            and rpc_method == "resources/read" and isinstance(req_obj, dict)
            and isinstance(req_obj.get("params"), dict)
            and req_obj["params"].get("uri") == _TERMINAL_MONITOR_V4)
        if v4_request:
            forwarded = {**req_obj, "params": {**req_obj["params"], "uri": _TERMINAL_MONITOR_V2}}
            body = json.dumps(forwarded, separators=(",", ":")).encode()
        url = target + request.rel_url.path_qs
        # Only the read-only terminal monitor resource is eligible. Everything
        # else uses the unmodified streaming capture proxy below.
        requested_uri = (req_obj.get("params") or {}).get("uri") if isinstance(req_obj, dict) and isinstance(req_obj.get("params"), dict) else None
        stylesheet = (
            _terminal_monitor_stylesheet()
            if slug == "terminal" and request.method == "POST"
            and rpc_method == "resources/read" and (requested_uri in _TERMINAL_MONITOR_RESOURCES or v4_request)
            else None
        )
        async with self.session.request(
            request.method, url, headers=headers, data=body, allow_redirects=False
        ) as upstream:
            out_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in HOP_BY_HOP}
            if stylesheet and upstream.status == 200:
                # resources/read is finite. Cap the buffered HTML response so
                # unexpectedly large upstream data remains a streaming pass-through.
                try:
                    raw_response = await upstream.content.readexactly(
                        _MAX_TERMINAL_MONITOR_RESOURCE_BYTES + 1
                    )
                except asyncio.IncompleteReadError as exc:
                    raw_response = exc.partial
                if len(raw_response) > _MAX_TERMINAL_MONITOR_RESOURCE_BYTES:
                    response = web.StreamResponse(
                        status=upstream.status, reason=upstream.reason, headers=out_headers
                    )
                    await response.prepare(request)
                    await response.write(raw_response)
                    async for chunk in upstream.content.iter_chunked(65536):
                        await response.write(chunk)
                    await response.write_eof()
                    return response
                modified, changed = _overlay_terminal_resource_response(
                    raw_response, upstream.headers.get("Content-Type", ""),
                    _TERMINAL_MONITOR_V2 if v4_request else requested_uri, stylesheet
                )
                if v4_request:
                    modified, remapped = _remap_terminal_monitor_v4_response(
                        modified, upstream.headers.get("Content-Type", ""), "resources/read"
                    )
                    changed = changed or remapped
                if changed:
                    for key in tuple(out_headers):
                        if key.lower() in {"etag", "content-md5"}:
                            del out_headers[key]
                # aiohttp may have decompressed the buffered payload; avoid
                # advertising the upstream's (now stale) Content-Encoding.
                for key in tuple(out_headers):
                    if key.lower() == "content-encoding":
                        del out_headers[key]
                return web.Response(body=modified, status=upstream.status, reason=upstream.reason, headers=out_headers)
            if (slug == "terminal" and request.method == "POST"
                and rpc_method in ("tools/list", "resources/list") and upstream.status == 200):
                raw = await upstream.read()
                if len(raw) <= _MAX_TERMINAL_MONITOR_RESOURCE_BYTES:
                    changed_body, was_changed = _remap_terminal_monitor_v4_response(
                        raw, upstream.headers.get("Content-Type", ""), rpc_method
                    )
                    if was_changed:
                        return web.Response(body=changed_body, status=upstream.status,
                            reason=upstream.reason, headers={k:v for k,v in out_headers.items()
                                if k.lower() not in ("content-encoding", "etag", "content-md5")})
                return web.Response(body=raw, status=upstream.status,reason=upstream.reason,
                    headers={k:v for k,v in out_headers.items() if k.lower()!="content-encoding"})
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
