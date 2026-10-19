"""Exact-target MCP UI stylesheet override with production-safe passthrough tests."""
import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import ClientSession, web
from app import capture

URI = 'ui://terminal-mcp/monitor-v3.html'
HTML = '<!doctype html><html><head><style>pre{height:58vh;max-height:72vh}</style></head><body><strong>Ubuntu Terminal Monitor</strong><pre id="output"></pre></body></html>'
CSS = 'body{height:100dvh;display:flex;flex-direction:column}pre{flex:1 1 auto;max-height:none}'


def rpc_response(uri=URI, html=HTML, mime='text/html;profile=mcp-app'):
    return {'jsonrpc': '2.0', 'id': 42, 'result': {'contents': [{'uri': uri, 'mimeType': mime, 'text': html}]}}


class PureRewriteTests(unittest.TestCase):
    def test_exact_resource_json(self):
        before = rpc_response()
        raw = json.dumps(before).encode()
        result, changed = capture._overlay_terminal_resource_response(raw, 'application/json', URI, CSS)
        self.assertTrue(changed)
        obj = json.loads(result)
        self.assertEqual(obj['id'], 42)
        self.assertEqual(obj['result']['contents'][0]['uri'], URI)
        self.assertIn('height:100dvh', obj['result']['contents'][0]['text'])
        self.assertIn('height:58vh', obj['result']['contents'][0]['text'])
        self.assertEqual(obj['result']['contents'][0]['text'].count('terminal-monitor-viewport-overlay'), 1)
        again, second = capture._overlay_terminal_resource_response(result, 'application/json', URI, CSS)
        self.assertFalse(second)
        self.assertEqual(again, result)

    def test_old_aliases(self):
        for version in (1, 2, 3):
            uri = f'ui://terminal-mcp/monitor-v{version}.html'
            raw = json.dumps(rpc_response(uri)).encode()
            _, changed = capture._overlay_terminal_resource_response(raw, 'application/json', uri, CSS)
            self.assertTrue(changed)

    def test_strict_non_targets_byte_identical(self):
        cases = [
            ('ui://different/monitor-v3.html', rpc_response()),
            (URI, rpc_response('ui://terminal-mcp/monitor-v2.html')),
            (URI, rpc_response(mime='text/plain')),
            (URI, rpc_response(html='<style></style>Other content')),
            (URI, {'jsonrpc': '2.0', 'id': 1, 'result': {'tools': []}}),
        ]
        for uri, obj in cases:
            raw = json.dumps(obj, indent=2).encode()
            result, changed = capture._overlay_terminal_resource_response(raw, 'application/json', uri, CSS)
            self.assertFalse(changed)
            self.assertEqual(result, raw)

    def test_corrupt_mcp_fallback(self):
        for mime, raw in [('application/json', b'not-json'), ('text/plain', b'ok'), ('text/event-stream', b'data: not-json\n\n')]:
            changed_body, changed = capture._overlay_terminal_resource_response(raw, mime, URI, CSS)
            self.assertFalse(changed)
            self.assertEqual(changed_body, raw)

    def test_sse_preserves_event_framing_and_unrelated_events(self):
        message = json.dumps(rpc_response())
        before = f': heartbeat\n\nevent: message\ndata: {message}\n\nevent: ping\ndata: {{"x":1}}\n\n'.encode()
        result, changed = capture._overlay_terminal_resource_response(before, 'text/event-stream', URI, CSS)
        self.assertTrue(changed)
        self.assertTrue(result.startswith(b': heartbeat\n\nevent: message\ndata: '))
        self.assertTrue(result.endswith(b'event: ping\ndata: {"x":1}\n\n'))
        self.assertIn(b'height:100dvh', result)

    def test_style_file_validation_and_hot_disable(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / 'monitor.css'
            f.write_text(CSS)
            with patch.dict(os.environ, {'MCP_OBS_TERMINAL_MONITOR_CSS_FILE': str(f)}):
                self.assertEqual(capture._terminal_monitor_stylesheet(), CSS)
                f.write_text('@import url(https://evil.example/)')
                self.assertIsNone(capture._terminal_monitor_stylesheet())
                f.write_text('<script>alert(1)</script>')
                self.assertIsNone(capture._terminal_monitor_stylesheet())
                f.write_bytes(b'x' * 9000)
                self.assertIsNone(capture._terminal_monitor_stylesheet())
                f.write_text(CSS)
                self.assertEqual(capture._terminal_monitor_stylesheet(), CSS)
                f.unlink()
                self.assertIsNone(capture._terminal_monitor_stylesheet())


class ProxyIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = sqlite3.connect(':memory:')
        # Observatory schema is normally provisioned by its inspector process.
        self.db.execute('CREATE TABLE requests(id INTEGER PRIMARY KEY)')
        capture.ensure_schema(self.db)
        self.override_file = tempfile.NamedTemporaryFile(mode='w+', suffix='.css', delete=False)
        self.override_file.write(CSS)
        self.override_file.flush()
        self.patchenv = patch.dict(os.environ, {'MCP_OBS_TERMINAL_MONITOR_CSS_FILE': self.override_file.name})
        self.patchenv.start()

        async def upstream_handler(request):
            data = await request.json()
            if data.get('method') == 'resources/read':
                resp = json.dumps(rpc_response(uri=data['params']['uri'])).encode()
                if request.headers.get('X-Fixture-Large') == '1':
                    return web.Response(body=resp + b'x' * (300 * 1024), content_type='application/json')
                if request.headers.get('X-Fixture-SSE') == '1':
                    return web.Response(body=b'event: message\ndata: ' + resp + b'\n\n', content_type='text/event-stream')
                return web.Response(body=resp, content_type='application/json')
            return web.Response(body=b'{"jsonrpc":"2.0","id":42,"result":{"tools":[]}}', content_type='application/json')

        app = web.Application()
        app.router.add_route('*', '/{tail:.*}', upstream_handler)
        self.upstream_runner = web.AppRunner(app)
        await self.upstream_runner.setup()
        self.upstream_site = web.TCPSite(self.upstream_runner, '127.0.0.1', 0)
        await self.upstream_site.start()
        upstream_port = self.upstream_site._server.sockets[0].getsockname()[1]
        self.patch_capture = patch.object(capture, 'HTTP_CAPTURE', {'terminal': {'target': f'http://127.0.0.1:{upstream_port}', 'listen_port': 0}})
        self.patch_capture.start()
        self.proxy = capture.CaptureProxyManager(self.db)
        await self.proxy.start()
        self.port = next(iter(self.proxy.runners[0].sites))._server.sockets[0].getsockname()[1]
        self.client = ClientSession()

    async def asyncTearDown(self):
        await self.client.close()
        await self.proxy.close()
        await self.upstream_runner.cleanup()
        self.patch_capture.stop()
        self.patchenv.stop()
        self.db.close()
        self.override_file.close()
        Path(self.override_file.name).unlink(missing_ok=True)

    async def post(self, method, uri=URI, sse=False):
        headers = {'Accept': 'application/json, text/event-stream'}
        if method == 'resources/read' and uri == 'large-test':
            headers['X-Fixture-Large'] = '1'
            uri = URI
        if sse:
            headers['X-Fixture-SSE'] = '1'
        async with self.client.post(f'http://127.0.0.1:{self.port}/mcp', headers=headers, json={'jsonrpc': '2.0', 'id': 42, 'method': method, 'params': {'uri': uri}}) as result:
            return result.status, result.headers.get('Content-Type'), await result.read()

    async def test_json_and_sse_and_passthrough(self):
        status, typ, raw = await self.post('resources/read')
        self.assertEqual(status, 200)
        self.assertIn('application/json', typ)
        self.assertIn('height:100dvh', json.loads(raw)['result']['contents'][0]['text'])
        status, typ, raw = await self.post('resources/read', sse=True)
        self.assertEqual(status, 200)
        self.assertIn('text/event-stream', typ)
        self.assertIn(b'height:100dvh', raw)
        status, typ, raw = await self.post('tools/list')
        self.assertEqual(raw, b'{"jsonrpc":"2.0","id":42,"result":{"tools":[]}}')
        status, typ, large = await self.post('resources/read', uri='large-test')
        self.assertEqual(status, 200)
        self.assertGreater(len(large), 256 * 1024)
        self.assertNotIn(b'terminal-monitor-viewport-overlay', large)
        # Removing CSS restores unmodified resources without any proxy restart.
        Path(self.override_file.name).unlink()
        _, _, raw = await self.post('resources/read')
        self.assertNotIn('height:100dvh', json.loads(raw)['result']['contents'][0]['text'])
        self.assertIn('height:58vh', json.loads(raw)['result']['contents'][0]['text'])

class VersionFourCacheTests(unittest.TestCase):
    def test_tool_metadata_and_resource_alias(self):
        base = {'jsonrpc':'2.0','id':1,'result':{'tools':[{'name':'terminal_monitor','_meta':{'openai/outputTemplate':'ui://terminal-mcp/monitor-v2.html','ui':{'resourceUri':'ui://terminal-mcp/monitor-v2.html'}}},{'name':'another','_meta':{'openai/outputTemplate':'ui://other/a.html'}}]}}
        blob, changed = capture._remap_terminal_monitor_v4_response(json.dumps(base).encode(),'application/json','tools/list')
        self.assertTrue(changed)
        tools = json.loads(blob)['result']['tools']
        self.assertEqual(tools[0]['_meta']['openai/outputTemplate'],capture._TERMINAL_MONITOR_V4)
        self.assertEqual(tools[0]['_meta']['ui']['resourceUri'],capture._TERMINAL_MONITOR_V4)
        self.assertEqual(tools[1],base['result']['tools'][1])
        listed={'jsonrpc':'2.0','id':1,'result':{'resources':[{'uri':'ui://terminal-mcp/monitor-v2.html','mimeType':'text/html;profile=mcp-app'}]}}
        buf, changed = capture._remap_terminal_monitor_v4_response(json.dumps(listed).encode(),'application/json','resources/list')
        self.assertTrue(changed)
        self.assertEqual(json.loads(buf)['result']['resources'][-1]['uri'],capture._TERMINAL_MONITOR_V4)
        read = rpc_response('ui://terminal-mcp/monitor-v2.html')
        buf, changed = capture._remap_terminal_monitor_v4_response(json.dumps(read).encode(),'application/json','resources/read')
        self.assertTrue(changed)
        self.assertEqual(json.loads(buf)['result']['contents'][0]['uri'],capture._TERMINAL_MONITOR_V4)
