"""Wait for local OAuth prerequisites and verify freshly started tunnel discovery."""
import argparse
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request


def local_base(value):
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "::1")
            or not parsed.port or parsed.path not in ("", "/")
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise argparse.ArgumentTypeError("Expected an HTTP loopback base URL with an explicit port")
    return value.rstrip("/")


def fetch(base, path, as_json=True):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(base + path, timeout=2) as response:
        body = response.read(65537)
        if response.status != 200 or len(body) > 65536:
            raise ValueError("Unexpected local readiness response")
    return json.loads(body) if as_json else body.decode().strip()


def gateway_ready(base):
    socket.getaddrinfo("api.openai.com", 443, type=socket.SOCK_STREAM)
    fetch(base, "/healthz")
    metadata = fetch(base, "/.well-known/oauth-protected-resource/mcp")
    return bool(metadata.get("resource") and metadata.get("authorization_servers"))


def tunnel_ready(base):
    readiness = fetch(base, "/readyz", as_json=False)
    if readiness != "ready" and not readiness.startswith("ready ("):
        return False
    oauth = fetch(base, "/api/oauth")
    status = fetch(base, "/api/status")
    return bool(not oauth.get("error") and oauth.get("metadata")
                and not status.get("tunnel_metadata_error") and status.get("tunnel_metadata"))


def wait_until(check, timeout, interval=0.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if check():
                return True
        except (OSError, ValueError, urllib.error.URLError):
            pass
        time.sleep(min(interval, max(0, deadline - time.monotonic())))
    return False


def main():
    parser = argparse.ArgumentParser()
    targets = parser.add_mutually_exclusive_group(required=True)
    targets.add_argument("--gateway-url", type=local_base)
    targets.add_argument("--tunnel-url", type=local_base)
    parser.add_argument("--timeout", type=float, default=45)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("timeout must be positive")
    check = (lambda: gateway_ready(args.gateway_url)) if args.gateway_url else (lambda: tunnel_ready(args.tunnel_url))
    if not wait_until(check, args.timeout):
        raise SystemExit("OAuth startup readiness not established; service startup must retry")
    print("OAuth startup readiness verified")


if __name__ == "__main__":
    main()
