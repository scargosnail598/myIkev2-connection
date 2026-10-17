#!/usr/bin/env python3
"""Validate origin settings and CDN networks before generating a Caddyfile."""

import ipaddress
import json
from pathlib import Path
import re
import sys


def render(settings: dict, networks_text: str) -> str:
    hostname = settings["hostname"]
    port = settings["port"]
    if not isinstance(hostname, str) or len(hostname) > 253 or not re.fullmatch(
        r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", hostname
    ):
        raise ValueError("Invalid origin hostname")
    if type(port) is not int or not 1024 <= port <= 65535 or port == 8787:
        raise ValueError("Origin port must be unprivileged and different from the backend port")
    networks = []
    for line in networks_text.splitlines():
        line = line.strip()
        if not line:
            continue
        network = ipaddress.ip_network(line, strict=True)
        if network.prefixlen == 0 or not network.is_global:
            raise ValueError("CDN ranges must be public networks with a nonzero prefix")
        networks.append(str(network))
    if not networks:
        raise ValueError("CDN network list is empty")
    ranges = " ".join(dict.fromkeys(networks))
    return f"""{{
    admin off
    auto_https off
}}

:{port} {{
    route {{
        @denied not remote_ip 127.0.0.1/32 ::1/128 {ranges}
        respond @denied "Forbidden" 403
        @wrong_host not host {hostname}
        respond @wrong_host "Not found" 404
        header Cache-Control "private, no-store"
        @insecure not header X-Forwarded-Proto https
        redir @insecure https://{hostname}{{uri}} 308
        reverse_proxy 127.0.0.1:8787 {{
            header_up Host {hostname}
            header_up X-Forwarded-Proto https
            header_up -X-Real-IP
            header_down Cache-Control "private, no-store"
        }}
    }}
}}
"""


if __name__ == "__main__":
    print(render(json.loads(Path(sys.argv[1]).read_text()), Path(sys.argv[2]).read_text()), end="")
