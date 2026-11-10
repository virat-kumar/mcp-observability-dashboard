import argparse
import importlib.util
from pathlib import Path
import socket
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("wait_mcp_startup", Path(__file__).resolve().parents[1] / "scripts/wait_mcp_startup.py")
startup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(startup)


class StartupTests(unittest.TestCase):
    def test_only_explicit_loopback_targets_allowed(self):
        self.assertEqual(startup.local_base("http://127.0.0.1:19012/"), "http://127.0.0.1:19012")
        for value in ("https://example.com", "http://127.0.0.1", "http://127.0.0.1:90/mcp", "http://user:password@127.0.0.1:90"):
            with self.assertRaises(argparse.ArgumentTypeError):
                startup.local_base(value)

    def test_dns_and_gateway_metadata_are_required(self):
        with patch.object(startup.socket, "getaddrinfo", return_value=[object()]), patch.object(startup, "fetch", side_effect=[{"ok": True}, {"resource": "private", "authorization_servers": ["issuer"]}]) as fetch:
            self.assertTrue(startup.gateway_ready("http://127.0.0.1:19012"))
            self.assertEqual(fetch.call_count, 2)
        with patch.object(startup.socket, "getaddrinfo", side_effect=socket.gaierror), patch.object(startup, "fetch") as fetch:
            with self.assertRaises(socket.gaierror):
                startup.gateway_ready("http://127.0.0.1:19012")
            fetch.assert_not_called()

    def test_failed_startup_is_retried_until_dependency_recovers(self):
        with patch.object(startup.time, "sleep"):
            attempts = iter([OSError("gateway unavailable"), False, True])
            def check():
                result = next(attempts)
                if isinstance(result, Exception):
                    raise result
                return result
            self.assertTrue(startup.wait_until(check, timeout=1))

    def test_persistent_failure_has_a_deadline(self):
        self.assertFalse(startup.wait_until(lambda: False, timeout=0.01, interval=0.001))

    def test_healthy_gateway_does_not_hide_stale_tunnel_metadata(self):
        for status in ({"tunnel_metadata_error": "DNS timed out"}, {"tunnel_metadata": {"id": "test"}}):
            with patch.object(startup, "fetch", side_effect=["ready", {"metadata": {"resource": "private"}}, status]):
                self.assertEqual(startup.tunnel_ready("http://127.0.0.1:18880"), "tunnel_metadata_error" not in status)

    def test_discovery_error_prevents_ready_state(self):
        with patch.object(startup, "fetch", side_effect=["ready", {"error": "discovery failed", "metadata": {}}, {"tunnel_metadata": {"id": "test"}}]):
            self.assertFalse(startup.tunnel_ready("http://127.0.0.1:18880"))
