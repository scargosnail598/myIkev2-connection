#!/usr/bin/env python3
"""Local-only Web UI for the managed IKEv2 server."""

from __future__ import annotations

import argparse
from http.cookies import SimpleCookie
import json
import mimetypes
import os
from pathlib import Path
import subprocess
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import parse_qs, urlparse

from webui.broker import BrokerClient, BrokerError, DEFAULT_SOCKET, installation_state
from webui.security import AuthConfig, AuthManager, Session


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = Path(__file__).resolve().parent / "static"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
DEFAULT_CONFIG = Path("/etc/ikev2-web/config.json")
SESSION_COOKIE = "ikev2_session"
MAX_REQUEST_BYTES = 4096

SERVICES = (
    ("ikev2", "IKEv2 / StrongSwan", "strongswan-starter.service"),
    ("firewall", "Firewall & NAT", "ikev2-vpn-firewall.service"),
    ("proxy", "SOCKS5 Proxy", "ikev2-vpn-proxy.service"),
)


class SystemStatusProvider:
    """Collect non-sensitive service state without privileged commands."""

    def __init__(
        self,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        broker: BrokerClient | None = None,
    ):
        self._runner = runner
        self._broker = broker

    def service_state(self, unit: str) -> str:
        try:
            result = self._runner(
                ["systemctl", "is-active", unit],
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
            )
        except (OSError, subprocess.TimeoutExpired):
            return "unknown"

        state = result.stdout.strip().lower()
        if state in {"active", "inactive", "failed", "activating", "deactivating"}:
            return state
        return "unknown"

    def dashboard(self) -> dict[str, object]:
        installed = installation_state()
        services = [
            {"id": key, "label": label, "unit": unit, "state": self.service_state(unit)}
            for key, label, unit in SERVICES
        ]
        broker_available = False
        user_management = False
        profile_export = False
        user_limits = False
        session_monitoring = False
        diagnostics = False
        metrics = False
        if self._broker:
            try:
                status = self._broker.status()
                broker_available = status.get("ok") is True
                metrics = broker_available and status.get("metrics") is True
                diagnostics = broker_available and status.get("diagnostics") is True
                session_monitoring = broker_available and status.get("session_monitoring") is True
                user_limits = broker_available and status.get("user_limits") is True
                profile_export = broker_available and status.get("profile_export") is True
                user_management = broker_available and status.get("user_management") is True
                if broker_available and isinstance(status.get("installed"), bool):
                    installed = status["installed"]
            except BrokerError:
                pass
        return {
            "installed": installed,
            "mode": "controlled" if broker_available else "read-only",
            "services": services,
            "capabilities": {
                "service_actions": broker_available,
                "user_management": user_management,
                "profile_export": profile_export,
                "user_limits": user_limits,
                "session_management": False,
                "session_monitoring": session_monitoring,
                "diagnostics": diagnostics,
                "metrics": metrics,
            },
        }


class Ikev2RequestHandler(BaseHTTPRequestHandler):
    server_version = "Ikev2Web/0.3"

    @property
    def provider(self) -> SystemStatusProvider:
        return self.server.status_provider  # type: ignore[attr-defined]

    @property
    def auth(self) -> AuthManager:
        return self.server.auth_manager  # type: ignore[attr-defined]

    @property
    def broker(self) -> BrokerClient:
        return self.server.broker_client  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        path = urlparse(self.path).path
        if path == "/api/health":
            self._send_json({"status": "ok", "version": "0.3.0"})
            return
        if path == "/api/session":
            session = self._current_session()
            self._send_json({
                "authenticated": session is not None,
                "username": session.username if session else None,
                "csrf_token": session.csrf_token if session else None,
            })
            return
        if path == "/api/dashboard":
            if self._require_session() is None:
                return
            self._send_json(self.provider.dashboard())
            return
        if path == "/api/metrics":
            if self._require_session() is None:
                return
            try:
                result = self.broker.metrics()
            except BrokerError:
                self._send_json({"message": "Server metrics are unavailable. Try refreshing metrics."}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            if result.get("ok") is True:
                self._send_json(result)
            else:
                self._send_json({"message": "Server metrics could not be read."}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        if path == "/api/diagnostics":
            if self._require_session() is None:
                return
            try:
                result = self.broker.diagnostics()
            except BrokerError:
                self._send_json({"message": "Server diagnostics are unavailable. Check the broker and try again."},
                                HTTPStatus.SERVICE_UNAVAILABLE)
                return
            if result.get("ok") is True:
                self._send_json(result)
            else:
                self._send_json({"message": "Server diagnostics could not be completed."}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        if path == "/api/sessions":
            if self._require_session() is None:
                return
            query = parse_qs(urlparse(self.path).query)
            try:
                page = int(query.get("page", ["0"])[0])
            except ValueError:
                self._send_json({"message": "Invalid session page."}, HTTPStatus.BAD_REQUEST)
                return
            try:
                result = self.broker.list_sessions(query.get("search", [""])[0], page)
            except BrokerError:
                self._send_json({"message": "VPN session monitoring is unavailable."}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            if result.get("ok") is True:
                self._send_json(result)
            else:
                self._send_json({"message": result.get("message", "VPN session monitoring is unavailable.")},
                                400 if result.get("status") == 400 else 503)
            return
        if path == "/api/users":
            if self._require_session() is None:
                return
            query = parse_qs(urlparse(self.path).query)
            try:
                page = int(query.get("page", ["0"])[0])
            except ValueError:
                self._send_json({"message": "Invalid user page."}, HTTPStatus.BAD_REQUEST)
                return
            self._user_result(lambda: self.broker.list_users(query.get("search", [""])[0], page))
            return
        if path == "/":
            self._send_file(STATIC_ROOT / "index.html")
            return
        if path.startswith("/assets/"):
            relative = path.removeprefix("/assets/")
            candidate = (STATIC_ROOT / relative).resolve()
            if candidate.parent != STATIC_ROOT.resolve():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self._send_file(candidate)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        path = urlparse(self.path).path
        if path == "/api/auth/login":
            self._login()
            return
        session = self._require_session()
        if session is None:
            return
        if not self.auth.csrf_valid(session, self.headers.get("X-CSRF-Token")):
            self._send_json(
                {"error": "csrf_failed", "message": "The CSRF token is missing or invalid."},
                status=HTTPStatus.FORBIDDEN,
            )
            return
        if path == "/api/auth/logout":
            token = self._session_token()
            self.auth.destroy_session(token)
            self._send_json(
                {"authenticated": False},
                cookies=[self._expired_session_cookie()],
            )
            return
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[:2] == ["api", "users"]:
            if parts[2] not in {"create", "reset", "delete", "export", "limits_set", "usage_reset"}:
                self._send_json({"message": "Unsupported user operation."}, HTTPStatus.BAD_REQUEST)
                return
            try:
                payload = self._read_json()
            except ValueError as error:
                self._send_json({"message": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            exporting = parts[2] == "export"
            limiting = parts[2] in {"limits_set", "usage_reset"}
            if limiting:
                allowed = {"username", "revision", "limits_revision"}
                if parts[2] == "limits_set":
                    allowed |= {"byte_limit", "validity_seconds"}
            else:
                allowed = {"username", "revision", "include_password"} if exporting else {"username", "password", "revision", "allow_last"}
            if set(payload) - allowed:
                self._send_json({"message": "Unsupported user fields."}, HTTPStatus.BAD_REQUEST)
                return
            if limiting:
                self._user_result(lambda: self.broker.manage_limits({**payload, "operation": parts[2]}))
            elif exporting:
                self._user_result(lambda: self.broker.export_user(payload))
            else:
                self._user_result(lambda: self.broker.manage_user({**payload, "operation": parts[2]}))
            return
        if len(parts) == 4 and parts[:2] == ["api", "services"]:
            _api, _services, target, operation = parts
            self._service_control(target, operation)
            return
        self._send_json(
            {"error": "write_operations_disabled", "message": "Privileged broker is not enabled."},
            status=HTTPStatus.METHOD_NOT_ALLOWED,
        )

    def _user_result(self, operation) -> None:
        try:
            result = operation()
        except BrokerError:
            self._send_json({"message": "VPN user management is unavailable."}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        if result.get("ok") is True:
            self._send_json(result)
        else:
            status = result.get("status", 503)
            if type(status) is not int or status not in {400, 404, 409, 503}:
                status = 503
            self._send_json({"error": result.get("error", "users_unavailable"),
                             "message": result.get("message", "VPN user management is unavailable.")}, status)

    def _service_control(self, target: str, operation: str) -> None:
        if target not in {"ikev2", "proxy", "all"} or operation not in {"start", "stop", "restart"}:
            self._send_json(
                {"error": "invalid_action", "message": "Unsupported service action."},
                HTTPStatus.BAD_REQUEST,
            )
            return
        try:
            result = self.broker.service_control(target, operation)
        except BrokerError as error:
            self._send_json(
                {"error": "broker_unavailable", "message": str(error)},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        if not result.get("ok"):
            self._send_json(
                {"error": "service_action_failed", "message": "The service action failed.", "result": result},
                HTTPStatus.BAD_GATEWAY,
            )
            return
        self._send_json(result)

    def _login(self) -> None:
        client_key = self.client_address[0]
        allowed, retry_after = self.auth.login_allowed(client_key)
        if not allowed:
            self._send_json(
                {"error": "rate_limited", "message": "Too many failed login attempts."},
                status=HTTPStatus.TOO_MANY_REQUESTS,
                headers={"Retry-After": str(retry_after)},
            )
            return
        try:
            payload = self._read_json()
        except ValueError as error:
            self._send_json({"error": "invalid_request", "message": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        username = payload.get("username")
        password = payload.get("password")
        if not isinstance(username, str) or not isinstance(password, str):
            self._send_json(
                {"error": "invalid_request", "message": "Username and password are required."},
                HTTPStatus.BAD_REQUEST,
            )
            return
        result = self.auth.authenticate(username, password, client_key)
        if result is None:
            allowed, retry_after = self.auth.login_allowed(client_key)
            headers = {"Retry-After": str(retry_after)} if not allowed else None
            status = HTTPStatus.TOO_MANY_REQUESTS if not allowed else HTTPStatus.UNAUTHORIZED
            self._send_json(
                {"error": "invalid_credentials", "message": "Invalid username or password."},
                status,
                headers=headers,
            )
            return
        token, session = result
        self._send_json(
            {"authenticated": True, "username": session.username, "csrf_token": session.csrf_token},
            cookies=[self._session_cookie(token)],
        )

    def _read_json(self) -> dict[str, object]:
        if self.headers.get_content_type() != "application/json":
            raise ValueError("Content-Type must be application/json.")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("Invalid Content-Length.") from error
        if length < 1 or length > MAX_REQUEST_BYTES:
            raise ValueError(f"Request body must contain between 1 and {MAX_REQUEST_BYTES} bytes.")
        try:
            payload = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ValueError("Request body must contain valid JSON.") from error
        if not isinstance(payload, dict):
            raise ValueError("JSON request body must be an object.")
        return payload

    def _session_token(self) -> str | None:
        cookie_header = self.headers.get("Cookie")
        if not cookie_header:
            return None
        try:
            cookie = SimpleCookie(cookie_header)
        except Exception:
            return None
        morsel = cookie.get(SESSION_COOKIE)
        return morsel.value if morsel else None

    def _current_session(self) -> Session | None:
        return self.auth.session(self._session_token())

    def _require_session(self) -> Session | None:
        session = self._current_session()
        if session is None:
            self._send_json(
                {"error": "authentication_required", "message": "Sign in to continue."},
                status=HTTPStatus.UNAUTHORIZED,
            )
        return session

    def _session_cookie(self, token: str) -> str:
        attributes = [
            f"{SESSION_COOKIE}={token}",
            "Path=/",
            "HttpOnly",
            "SameSite=Strict",
            f"Max-Age={self.auth.config.session_ttl_seconds}",
        ]
        if self.auth.config.secure_cookie:
            attributes.append("Secure")
        return "; ".join(attributes)

    def _expired_session_cookie(self) -> str:
        attributes = [
            f"{SESSION_COOKIE}=",
            "Path=/",
            "HttpOnly",
            "SameSite=Strict",
            "Max-Age=0",
        ]
        if self.auth.config.secure_cookie:
            attributes.append("Secure")
        return "; ".join(attributes)

    def _send_json(
        self,
        payload: object,
        status: HTTPStatus = HTTPStatus.OK,
        headers: dict[str, str] | None = None,
        cookies: list[str] | None = None,
    ) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        for cookie in cookies or []:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = path.read_bytes()
        content_type, _ = mimetypes.guess_type(path.name)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type or 'application/octet-stream'}; charset=utf-8")
        self.send_header("Cache-Control", "no-store" if path.name == "index.html" else "public, max-age=300")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format_string: str, *args: object) -> None:
        print(f"{self.address_string()} - {format_string % args}")


class Ikev2WebServer(ThreadingHTTPServer):
    def __init__(
        self,
        address: tuple[str, int],
        provider: SystemStatusProvider,
        auth_manager: AuthManager,
        broker_client: BrokerClient,
    ):
        super().__init__(address, Ikev2RequestHandler)
        self.status_provider = provider
        self.auth_manager = auth_manager
        self.broker_client = broker_client


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local-only IKEv2 administration dashboard")
    parser.add_argument("--host", default=os.environ.get("IKEV2_WEB_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.environ.get("IKEV2_WEB_PORT", DEFAULT_PORT)))
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("IKEV2_WEB_CONFIG", DEFAULT_CONFIG)),
        help=f"authentication config (default: {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--broker-socket",
        type=Path,
        default=Path(os.environ.get("IKEV2_WEB_BROKER_SOCKET", DEFAULT_SOCKET)),
        help=f"privileged broker socket (default: {DEFAULT_SOCKET})",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        raise SystemExit("Refusing a non-loopback bind. Put an HTTPS reverse proxy in front of loopback.")
    try:
        auth_config = AuthConfig.from_file(args.config)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    broker = BrokerClient(args.broker_socket)
    server = Ikev2WebServer(
        (args.host, args.port),
        SystemStatusProvider(broker=broker),
        AuthManager(auth_config),
        broker,
    )
    print(f"IKEv2 Web UI listening on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
