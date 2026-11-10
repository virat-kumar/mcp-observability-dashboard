import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
TEMPORARY = tempfile.TemporaryDirectory()
with patch.dict(os.environ, {"MCP_OBS_DB": str(Path(TEMPORARY.name) / "test.db"), "MCP_OBS_DEBUG_LOGS": "false"}):
    from app import main


class InventoryTests(unittest.TestCase):
    def test_inventory_keeps_existing_identity_and_combines_retired_versions(self):
        self.assertEqual(main.TUNNELS["terminal"]["port"], 18880)
        self.assertEqual(len(main.TUNNELS), 14)
        for slug in ("terminal", "playwright", "computer", "personal", "excel", "background"):
            self.assertIn(slug, main.TUNNELS)
        ports = [config["port"] for config in main.TUNNELS.values()]
        self.assertEqual(len(ports), len(set(ports)))
        self.assertFalse({8080, 8901, 8912}.intersection(ports))
        self.assertTrue(any("English" in name for name in main.TUNNELS["terminal"]["deprecated_versions"]))

    def test_prometheus_generated_from_current_inventory(self):
        spec = importlib.util.spec_from_file_location("prometheus_config", ROOT / "scripts/generate_prometheus_config.py")
        generator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(generator)
        inventory = json.loads((ROOT / "app/mcp_inventory.json").read_text())
        expected = generator.render(inventory)
        self.assertEqual((ROOT / "prometheus/prometheus.yml").read_text(), expected)
        self.assertEqual(expected.count("labels: {mcp:"), 12)
        self.assertNotIn("127.0.0.1:8080", expected)


class CollectorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.collector = main.Collector()
        await self.collector.client.aclose()

    async def asyncTearDown(self):
        await self.collector.close()

    async def test_oauth_ready_explanation_is_not_reported_down(self):
        requests = []

        def handler(request):
            requests.append(request)
            if request.url.path == "/healthz": return httpx.Response(200, text="live")
            if request.url.path == "/readyz": return httpx.Response(200, text="ready (mcp initialize requires auth: caller JWT)")
            if request.url.path == "/metrics": return httpx.Response(200, text="")
            if request.url.path == "/api/logs": return httpx.Response(200, json={"events": None})
            return httpx.Response(200, json={})

        self.collector.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await self.collector.collect_one("inventory_test_tunnel", {"base": "http://fixture", "port": 1})
        row = main.DB.execute("SELECT healthy,ready FROM snapshots WHERE mcp='inventory_test_tunnel' ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(tuple(row), (1, 1))
        self.assertNotIn("inventory_test_tunnel", self.collector.latest_error)
        self.assertFalse(any(request.method != "GET" for request in requests))

    async def test_health_only_does_not_probe_tools_or_invent_latency(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"status": "ok", "database": True})

        self.collector.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await self.collector.collect_one("inventory_test_local", {"base": "http://fixture", "monitoring": "health_only"})
        self.assertEqual([request.url.path for request in requests], ["/healthz"])
        row = main.DB.execute("SELECT healthy,ready,tool_calls_total FROM snapshots WHERE mcp='inventory_test_local' ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(tuple(row), (1, 0, None))

    async def test_failed_oauth_readiness_is_visible(self):
        def handler(request):
            if request.url.path == "/healthz": return httpx.Response(200, text="live")
            if request.url.path == "/readyz": return httpx.Response(503, text="oauth discovery failed")
            if request.url.path == "/metrics": return httpx.Response(200, text="")
            return httpx.Response(200, json={})

        self.collector.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await self.collector.collect_one("inventory_test_oauth", {"base": "http://fixture"})
        self.assertEqual(self.collector.latest_status["inventory_test_oauth"]["readiness_error"], "oauth discovery failed")

    async def test_failed_probe_does_not_leave_stale_healthy_badge(self):
        response_status = 200

        def handler(request):
            return httpx.Response(response_status, json={"status": "ok"})

        config = {"name": "Test MCP", "port": 1, "base": "http://fixture", "monitoring": "health_only"}
        self.collector.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await self.collector.collect_one("inventory_test_failed", config)
        response_status = 503
        await self.collector.collect_one("inventory_test_failed", config)
        with patch.object(main, "collector", self.collector), patch.object(main, "TUNNELS", {"inventory_test_failed": config}):
            rows = (await main.health_api())["rows"]
        self.assertFalse(rows[0]["healthy"])
        self.assertFalse(rows[0]["ready"])
        self.assertTrue(rows[0]["error"])
