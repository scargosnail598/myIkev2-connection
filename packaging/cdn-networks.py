#!/usr/bin/env python3
"""Fetch public CDN ranges and transactionally refresh the managed origin proxy."""

import argparse
from contextlib import contextmanager
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import runpy
import stat
import subprocess
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

RENDERER = runpy.run_path(str(Path(__file__).with_name("render-cdn-proxy.py")))
render = RENDERER["render"]
parse_networks = RENDERER["parse_networks"]
PROVIDERS = {
    "arvancloud": "https://www.arvancloud.ir/fa/ips.txt",
    "cloudflare": "https://api.cloudflare.com/client/v4/ips",
}
CONFIG_DIR = Path("/etc/ikev2-web-proxy")
CADDY = "/usr/local/lib/ikev2-web/caddy"
SERVICE = "ikev2-web-proxy.service"
OWNER_UID = 0
MAX_BYTES = 1024 * 1024
MAX_NETWORKS = 8192


def validate_url(url):
    if not isinstance(url, str) or any(c.isspace() for c in url):
        raise ValueError("Use a public HTTPS feed URL")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or parsed.port not in (None, 443)):
        raise ValueError("Use a public HTTPS feed without credentials, query or fragment")
    host = parsed.hostname.lower()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host or host.endswith((".local", ".localhost")):
            raise ValueError("The feed must use a public hostname") from None
    else:
        if not address.is_global:
            raise ValueError("The feed must use a public address")
    return url


def network_source(settings):
    source = settings.get("network_source")
    if source is None:
        return None
    if not isinstance(source, dict):
        raise ValueError("Invalid CDN network source")
    provider = source.get("provider")
    if not isinstance(provider, str):
        raise ValueError("Invalid CDN provider")
    if provider in PROVIDERS and set(source) == {"provider"}:
        return {"provider": provider}
    if provider == "custom" and set(source) == {"provider", "url"}:
        return {"provider": provider, "url": validate_url(source["url"])}
    raise ValueError("Unknown CDN network source")


class HttpsRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_ranges(source):
    source = network_source({"network_source": source})
    if source is None:
        raise ValueError("Select an automatic CDN source")
    url = validate_url(PROVIDERS.get(source["provider"]) or source["url"])
    request = Request(url, headers={"User-Agent": "ikev2-cdn-networks/1", "Accept": "application/json, text/plain"})
    try:
        with build_opener(HttpsRedirects()).open(request, timeout=20) as response:
            data = response.read(MAX_BYTES + 1)
    except OSError:
        raise RuntimeError("CDN feed download failed; existing ranges were preserved") from None
    if len(data) > MAX_BYTES:
        raise ValueError("CDN feed exceeds the size limit")
    text = data.decode("utf-8")
    if source["provider"] == "cloudflare":
        payload = json.loads(text)
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise ValueError("Cloudflare did not return a successful IP list")
        result = payload["result"]
        if not isinstance(result, dict):
            raise ValueError("Invalid Cloudflare result")
        ipv4, ipv6 = result["ipv4_cidrs"], result["ipv6_cidrs"]
        if (not isinstance(ipv4, list) or not ipv4 or not isinstance(ipv6, list) or not ipv6
                or any(not isinstance(item, str) for item in ipv4 + ipv6)):
            raise ValueError("Cloudflare returned an incomplete IP list")
        if any(ipaddress.ip_network(item, strict=True).version != 4 for item in ipv4):
            raise ValueError("Invalid IPv4 feed")
        if any(ipaddress.ip_network(item, strict=True).version != 6 for item in ipv6):
            raise ValueError("Invalid IPv6 feed")
        text = "\n".join(ipv4 + ipv6)
    networks = parse_networks(text)
    if len(networks) > MAX_NETWORKS:
        raise ValueError("CDN feed contains too many networks")
    networks.sort(key=lambda item: (ipaddress.ip_network(item).version,
                                   int(ipaddress.ip_network(item).network_address),
                                   ipaddress.ip_network(item).prefixlen))
    return "\n".join(networks) + "\n"


def atomic_write(path, data):
    descriptor, temporary = tempfile.mkstemp(prefix=".networks-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            os.fchmod(output.fileno(), 0o644)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def secure_read(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != OWNER_UID or stat.S_IMODE(info.st_mode) != 0o644:
            raise ValueError("Proxy settings must be root-owned regular files, mode 0644")
        data = source.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError("Proxy file exceeds the size limit")
        return data


@contextmanager
def configuration_lock(directory):
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != OWNER_UID or stat.S_IMODE(info.st_mode) != 0o755:
        raise ValueError("Proxy directory must be root-owned, mode 0755")
    descriptor = os.open(directory / ".networks.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != OWNER_UID or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError("Unsafe CDN configuration lock")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def command(arguments, check=True):
    return subprocess.run(arguments, check=check, capture_output=True, timeout=30)


def proxy_health(settings):
    request = Request(f'http://127.0.0.1:{settings["port"]}/api/health',
                      headers={"Host": settings["hostname"], "X-Forwarded-Proto": "https"})
    for attempt in range(5):
        try:
            with urlopen(request, timeout=2) as response:
                if response.status == 200 and json.loads(response.read(4096)).get("status") == "ok":
                    return
        except (OSError, ValueError):
            pass
        if attempt < 4:
            time.sleep(0.2)
    raise RuntimeError("Proxy health failed after network refresh")


def refresh(directory=CONFIG_DIR, caddy=CADDY):
    with configuration_lock(directory):
        settings = json.loads(secure_read(directory / "origin.json"))
        source = network_source(settings)
        if source is None:
            print("Manual CDN list; automatic refresh is disabled.")
            return False
        if command(["systemctl", "is-active", "--quiet", SERVICE], check=False).returncode != 0:
            print("CDN proxy is stopped; no refresh applied.")
            return False
        previous_ranges = secure_read(directory / "cdn-ips.txt")
        previous_config = secure_read(directory / "Caddyfile")
        networks = fetch_ranges(source).encode()
        config = render(settings, networks.decode()).encode()
        if networks == previous_ranges and config == previous_config:
            print("CDN ranges are unchanged.")
            return False
        with tempfile.TemporaryDirectory(prefix="ikev2-cdn-check-") as stage:
            candidate = Path(stage) / "Caddyfile"
            candidate.write_bytes(config)
            command([caddy, "validate", "--config", str(candidate), "--adapter", "caddyfile"])
        try:
            atomic_write(directory / "cdn-ips.txt", networks)
            atomic_write(directory / "Caddyfile", config)
            command(["systemctl", "restart", SERVICE])
            proxy_health(settings)
        except Exception:
            failures = []
            for name, data in (("cdn-ips.txt", previous_ranges), ("Caddyfile", previous_config)):
                try:
                    atomic_write(directory / name, data)
                except OSError:
                    failures.append(name)
            try:
                command(["systemctl", "restart", SERVICE])
                proxy_health(settings)
            except (OSError, RuntimeError, subprocess.SubprocessError):
                failures.append("proxy restart")
            if failures:
                raise RuntimeError("CDN refresh and rollback failed; inspect proxy settings/service") from None
            raise RuntimeError("CDN refresh failed; previous ranges and proxy configuration restored") from None
        print(f'CDN ranges refreshed from {source["provider"]}; proxy health verified.')
        return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    fetch = actions.add_parser("fetch")
    fetch.add_argument("--provider", choices=(*PROVIDERS, "custom"), required=True)
    fetch.add_argument("--url")
    fetch.add_argument("--settings", type=Path, required=True)
    fetch.add_argument("--output", type=Path, required=True)
    mode = actions.add_parser("source")
    mode.add_argument("settings", type=Path)
    keep = actions.add_parser("keep-source")
    keep.add_argument("previous", type=Path)
    keep.add_argument("settings", type=Path)
    actions.add_parser("refresh")
    args = parser.parse_args()
    if args.action == "fetch":
        source = {"provider": args.provider}
        if args.provider == "custom":
            source["url"] = args.url
        settings = json.loads(args.settings.read_text())
        networks = fetch_ranges(source)
        render(settings, networks)
        settings["network_source"] = network_source({"network_source": source})
        atomic_write(args.output, networks.encode())
        atomic_write(args.settings, (json.dumps(settings) + "\n").encode())
        print(f'Validated {len(networks.splitlines())} CDN networks from {args.provider}.')
    elif args.action == "source":
        source = network_source(json.loads(args.settings.read_text()))
        print(source["provider"] if source else "manual")
    elif args.action == "keep-source":
        source = network_source(json.loads(args.previous.read_text()))
        settings = json.loads(args.settings.read_text())
        if source:
            settings["network_source"] = source
        atomic_write(args.settings, (json.dumps(settings) + "\n").encode())
    else:
        if os.geteuid() != 0:
            raise ValueError("Automatic CDN refresh requires root")
        refresh()


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError, subprocess.SubprocessError) as error:
        raise SystemExit(f"CDN networks: {error}") from None
