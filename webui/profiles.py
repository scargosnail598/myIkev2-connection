"""Portable, offline configuration sharing. No private keys or files are exported."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import shlex
import ssl
import zlib

from webui.users import UserError, read_private

CA_CERT = Path("/etc/ipsec.d/cacerts/ikev2-installer-ca-cert.pem")
SHARE_PREFIX = "ikev://import/v1/"
MAX_SHARE_BYTES = 2953  # QR version 40, byte mode, low error correction.
STATE_FIELDS = {"SERVER_ID", "CERT_MODE", "CERTIFICATE_MODE", "IPV6_ENABLED",
                "ALLOW_STOCK_WINDOWS", "PROXY_ENABLED", "PROXY_IP", "PROXY_PORT"}


def configuration_error():
    return UserError("profile_unavailable", "Managed VPN settings or CA certificate cannot be exported. Check the server installation.", 503)


def read_settings(path):
    # Read only known scalar assignments. Never source installer state as shell code.
    settings = {}
    try:
        for line in read_private(path).decode("utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key in STATE_FIELDS:
                parts = shlex.split(value)
                if key in settings or len(parts) != 1:
                    raise ValueError("Invalid assignment")
                settings[key] = parts[0]
        server = settings.get("SERVER_ID", "")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", server):
            raise ValueError("Invalid server")
        trust = settings.get("CERTIFICATE_MODE")
        if trust is None:
            mode = settings.get("CERT_MODE", "private")
            trust = {"private": "private-ca", "public": "public-trust"}.get(mode)
        if trust not in {"private-ca", "public-trust"}:
            raise ValueError("Invalid certificate mode")
        for key in ("IPV6_ENABLED", "ALLOW_STOCK_WINDOWS", "PROXY_ENABLED"):
            if settings.get(key, "no") not in {"yes", "no"}:
                raise ValueError("Invalid flag")
        settings["trust"] = trust
        return settings
    except (OSError, UnicodeError, ValueError) as error:
        raise configuration_error() from error


def build_share(state, username, password=None, certificate=CA_CERT):
    settings = read_settings(state)
    server = settings["SERVER_ID"]
    profile = {
        "format": "ikev-profile", "version": 1, "name": f"{username}@{server}",
        "server": server, "remote_id": server, "username": username,
        "authentication": "eap-mschapv2",
        "certificate_trust": "public" if settings["trust"] == "public-trust" else "private-ca",
        "connection": {"mode": "full-tunnel", "ipv6": settings.get("IPV6_ENABLED") == "yes"},
        "server_profile": "stock-windows-compatible" if settings.get("ALLOW_STOCK_WINDOWS") == "yes" else "secure",
        "proxy": {"enabled": False},
    }
    try:
        if settings["trust"] == "private-ca":
            # This fixed public certificate path is the only certificate we read.
            # PEM conversion accepts no keys; fail closed if the CA is unavailable.
            with certificate.open("r", encoding="ascii") as source:
                pem = source.read(16385)
            if len(pem) > 16384 or not re.fullmatch(
                    r"\s*-----BEGIN CERTIFICATE-----[A-Za-z0-9+/=\s]+-----END CERTIFICATE-----\s*", pem):
                raise ValueError("Invalid CA certificate")
            ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(cadata=pem)
            der = ssl.PEM_cert_to_DER_cert(pem.strip())
            if not der:
                raise ValueError("Empty CA certificate")
            profile["ca_certificate"] = {
                "encoding": "der-base64", "data": base64.b64encode(der).decode("ascii"),
                "sha256": ":".join(f"{byte:02X}" for byte in hashlib.sha256(der).digest()),
            }
        if settings.get("PROXY_ENABLED") == "yes":
            host = str(ipaddress.ip_address(settings["PROXY_IP"]))
            port = int(settings["PROXY_PORT"])
            if not 1 <= port <= 65535:
                raise ValueError("Invalid port")
            profile["proxy"] = {"enabled": True, "type": "socks5", "host": host, "port": port}
    except (OSError, UnicodeError, ValueError, KeyError) as error:
        raise configuration_error() from error
    envelope = {"format": "ikev-share", "version": 1, "profile": profile}
    if password is not None:
        envelope["password"] = password
    raw = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    encoded = base64.urlsafe_b64encode(zlib.compress(raw, level=9)).rstrip(b"=").decode("ascii")
    uri = SHARE_PREFIX + encoded
    if len(uri) > MAX_SHARE_BYTES:
        raise UserError("profile_too_large", "This profile exceeds QR capacity. Use the installer's .ikev file export instead.", 409)
    return {"ok": True, "uri": uri, "includes_password": password is not None}
