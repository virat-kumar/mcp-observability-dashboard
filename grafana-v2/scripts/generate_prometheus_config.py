"""Generate scrape targets from the same inventory used by the inspector."""
import argparse
import json
from pathlib import Path


def render(inventory):
    lines = [
        "global:", "  scrape_interval: 15s", "  evaluation_interval: 15s",
        "  scrape_timeout: 5s", "scrape_configs:", "  - job_name: mcp-tunnels",
        "    metrics_path: /metrics", "    static_configs:",
    ]
    for slug, config in inventory.items():
        if config.get("monitoring") == "health_only":
            continue
        lines.extend([
            f"      - targets: [127.0.0.1:{config['port']}]",
            f"        labels: {{mcp: {slug}}}",
        ])
    lines.extend([
        "  - job_name: mcp-observer-v2", "    static_configs:",
        "      - targets: [127.0.0.1:9120]",
    ])
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    inventory = json.loads((root / "app/mcp_inventory.json").read_text())
    config_path = root / "prometheus/prometheus.yml"
    expected = render(inventory)
    if arguments.check:
        if config_path.read_text() != expected:
            raise SystemExit("Prometheus targets differ from the MCP inventory; regenerate them")
    else:
        config_path.write_text(expected)


if __name__ == "__main__":
    main()
