#!/usr/bin/env python3
"""Create the Cloudflare tunnel for hyrox.miloprogressive.fit and start it on the Pi.

Idempotent: re-running reuses an existing tunnel and updates DNS in place.

    ./scripts/setup_tunnel.py

Reads the API token from $CLOUDFLARE_API_TOKEN, else `cs milo -secrets get
CLOUDFLARE_API_TOKEN`. The token is never printed.

Needs: Account > Cloudflare Tunnel > Edit, and Zone > DNS > Edit on the zone.

Written in Python rather than bash on purpose -- the JSON payloads carry braces
and quotes that shell word-splitting mangles.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request

ZONE = "miloprogressive.fit"
HOST = "hyrox." + ZONE
TUNNEL_NAME = "hyrox"
SERVICE = "http://hyrox:8000"
PI = "admin@raspberrypi.local"
PI_DIR = "/home/admin/Projects/hyrox"
API = "https://api.cloudflare.com/client/v4"


def token() -> str:
    value = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
    if value:
        return value
    result = subprocess.run(
        ["cs", "milo", "-secrets", "get", "CLOUDFLARE_API_TOKEN"],
        capture_output=True,
        text=True,
    )
    value = result.stdout.strip()
    if not value:
        sys.exit("no Cloudflare API token available (set CLOUDFLARE_API_TOKEN)")
    return value


CF_TOKEN = token()


def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(API + path, data=data, method=method)
    request.add_header("Authorization", "Bearer " + CF_TOKEN)
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        sys.exit("%s %s failed: HTTP %s\n%s" % (method, path, exc.code, detail))
    if not payload.get("success", False):
        sys.exit("%s %s returned errors:\n%s" % (method, path, json.dumps(payload.get("errors"), indent=2)))
    return payload["result"]


def main() -> int:
    print("==> verifying token")
    print("    status:", api("GET", "/user/tokens/verify")["status"])

    print("==> resolving account and zone")
    account_id = api("GET", "/accounts")[0]["id"]
    zones = api("GET", "/zones?name=" + ZONE)
    if not zones:
        sys.exit("zone %s not visible to this token -- it likely lacks Zone:DNS:Edit" % ZONE)
    zone_id = zones[0]["id"]
    print("    account %s…  zone %s…" % (account_id[:8], zone_id[:8]))

    print("==> finding or creating tunnel %r" % TUNNEL_NAME)
    existing = api(
        "GET",
        "/accounts/%s/cfd_tunnel?name=%s&is_deleted=false" % (account_id, TUNNEL_NAME),
    )
    if existing:
        tunnel_id = existing[0]["id"]
        print("    reusing", tunnel_id)
    else:
        tunnel_secret = base64.b64encode(secrets.token_bytes(32)).decode()
        tunnel_id = api(
            "POST",
            "/accounts/%s/cfd_tunnel" % account_id,
            {"name": TUNNEL_NAME, "tunnel_secret": tunnel_secret, "config_src": "cloudflare"},
        )["id"]
        print("    created", tunnel_id)

    print("==> routing %s -> %s" % (HOST, SERVICE))
    api(
        "PUT",
        "/accounts/%s/cfd_tunnel/%s/configurations" % (account_id, tunnel_id),
        {
            "config": {
                "ingress": [
                    {"hostname": HOST, "service": SERVICE},
                    {"service": "http_status:404"},
                ]
            }
        },
    )
    print("    ingress set")

    print("==> DNS record")
    record = {
        "type": "CNAME",
        "name": HOST,
        "content": tunnel_id + ".cfargotunnel.com",
        "proxied": True,
    }
    found = api("GET", "/zones/%s/dns_records?name=%s" % (zone_id, HOST))
    if found:
        api("PUT", "/zones/%s/dns_records/%s" % (zone_id, found[0]["id"]), record)
        print("    updated CNAME")
    else:
        api("POST", "/zones/%s/dns_records" % zone_id, record)
        print("    created CNAME")

    print("==> installing connector token on the Pi and starting cloudflared")
    connector = api("GET", "/accounts/%s/cfd_tunnel/%s/token" % (account_id, tunnel_id))
    remote = (
        "cd {dir} && python3 - <<'EOF'\n"
        "import pathlib\n"
        "p = pathlib.Path('secrets/secrets.env')\n"
        "lines = [l for l in p.read_text().splitlines() if not l.startswith('TUNNEL_TOKEN=')]\n"
        "lines.append('TUNNEL_TOKEN=' + {token!r})\n"
        "p.write_text('\\n'.join(lines) + '\\n')\n"
        "EOF\n"
        "chmod 600 secrets/secrets.env && "
        "docker compose up -d cloudflared && sleep 8 && "
        "docker compose ps --format '{{{{.Service}}}} {{{{.State}}}}'"
    ).format(dir=PI_DIR, token=connector)
    subprocess.run(["ssh", PI, remote], check=True)

    print("==> checking https://%s/healthz" % HOST)
    for attempt in range(1, 8):
        try:
            with urllib.request.urlopen("https://%s/healthz" % HOST, timeout=10) as response:
                if response.status == 200:
                    print("    200 OK — live at https://%s" % HOST)
                    return 0
        except Exception as exc:  # noqa: BLE001 -- DNS and the tunnel both need a moment
            print("    attempt %d: %s" % (attempt, exc.__class__.__name__))
        time.sleep(10)

    print("    not answering yet. Check:")
    print("      ssh %s 'cd %s && docker compose logs --tail 40 cloudflared'" % (PI, PI_DIR))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
