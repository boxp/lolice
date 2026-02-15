#!/usr/bin/env python3
"""Fetch Prometheus metrics from Grafana datasource proxy API."""

import argparse
import json
import os
import sys
from urllib.parse import urlparse

import requests

DEFAULT_DATASOURCE_ID = "1"


def _is_url_allowed(url: str, allowed_hosts: set[str]) -> bool:
    """Return True if the URL scheme/host combination is permitted.

    Allowed:
      - Any scheme when hostname is localhost or 127.0.0.1
      - Any scheme when hostname ends with .svc.cluster.local (k8s internal)
      - HTTPS when hostname is in the explicit *allowed_hosts* set
    """
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    scheme = parsed.scheme

    if scheme not in ("http", "https"):
        return False

    if hostname in ("localhost", "127.0.0.1"):
        return True

    if hostname.endswith(".svc.cluster.local"):
        return True

    if scheme == "https" and hostname in allowed_hosts:
        return True

    return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Query Prometheus metrics via Grafana datasource proxy"
    )
    parser.add_argument("prom_query", help="Prometheus query string")
    parser.add_argument(
        "--datasource-id",
        default=os.environ.get("GRAFANA_DATASOURCE_ID", DEFAULT_DATASOURCE_ID),
        help="Grafana datasource ID (default: %(default)s, env: GRAFANA_DATASOURCE_ID)",
    )
    args = parser.parse_args()

    grafana_url = os.environ.get("GRAFANA_URL")
    if not grafana_url:
        print("Error: GRAFANA_URL environment variable is not set", file=sys.stderr)
        return 1

    allowed_hosts_raw = os.environ.get("GRAFANA_ALLOWED_HOSTS", "")
    allowed_hosts = {h.strip() for h in allowed_hosts_raw.split(",") if h.strip()}

    if not _is_url_allowed(grafana_url, allowed_hosts):
        print(
            "Error: GRAFANA_URL must target localhost, a cluster-internal service, "
            "or an HTTPS host listed in GRAFANA_ALLOWED_HOSTS",
            file=sys.stderr,
        )
        return 1

    api_key = os.environ.get("GRAFANA_API_KEY")
    if not api_key:
        print(
            "Error: GRAFANA_API_KEY environment variable is not set", file=sys.stderr
        )
        return 1

    ds_id = args.datasource_id
    if not ds_id.isdigit() or int(ds_id) < 1:
        print("Error: datasource ID must be a positive integer", file=sys.stderr)
        return 1

    url = f"{grafana_url.rstrip('/')}/api/datasources/proxy/{ds_id}/api/v1/query"
    headers = {"Authorization": f"Bearer {api_key}"}
    params = {"query": args.prom_query}

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    try:
        data = resp.json()
    except json.JSONDecodeError:
        print("Error: response is not valid JSON", file=sys.stderr)
        return 1

    if not isinstance(data, dict) or data.get("status") != "success":
        error_msg = (
            data.get("error", "unexpected response")
            if isinstance(data, dict)
            else "unexpected response format"
        )
        print(f"Error: Prometheus query failed: {error_msg}", file=sys.stderr)
        return 1

    json.dump(data, sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
