#!/usr/bin/env python3
"""Root-only, allowlisted service broker for the unprivileged Web UI."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import grp
import json
import os
from pathlib import Path
import pwd
import socket
import socketserver
import stat
import struct
import subprocess
import threading
from typing import Callable

from webui.diagnostics import DiagnosticsReader
from webui.sessions import SessionReader
from webui.users import UserError, UserManager, validate_change


PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 4096
MAX_RESPONSE_BYTES = 65536
DEFAULT_SOCKET = Path("/run/ikev2-web/broker.sock")
DEFAULT_AUDIT_LOG = Path("/var/log/ikev2-web/audit.log")
SYSTEMCTL = "/usr/bin/systemctl"
MANAGED_STATE_FILE = Path("/var/lib/ikev2-easy-installer/state.env")

SERVICE_UNITS = {
    "ikev2": "strongswan-starter.service",
    "firewall": "ikev2-vpn-firewall.service",
    "proxy": "ikev2-vpn-proxy.service",
}
OPERATIONS = {"start", "stop", "restart"}


class BrokerError(Exception):
    """A safe-to-return broker error."""


def installation_state() -> bool | None:
    """Report presence only; never read or expose the root-only state contents."""
    try:
        return MANAGED_STATE_FILE.is_file()
    except OSError:
        return None


@dataclass(frozen=True)
class CommandResult:
    unit: str
    operation: str
    returncode: int
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {
            "unit": self.unit,
            "operation": self.operation,
            "ok": self.returncode == 0,
            "detail": self.detail,
        }


class ServiceController:
    """Translate a tiny action vocabulary into fixed systemctl argv arrays."""

    def __init__(
        self,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        systemctl: str = SYSTEMCTL,
    ):
        self._runner = runner
        self._systemctl = systemctl
        self._operation_lock = threading.Lock()

    def execute(self, target: str, operation: str) -> dict[str, object]:
        if target not in {"ikev2", "proxy", "all"}:
            raise BrokerError("Unsupported service target.")
        if operation not in OPERATIONS:
            raise BrokerError("Unsupported service operation.")

        commands = self._commands(target, operation)
        results: list[CommandResult] = []
        with self._operation_lock:
            for unit, unit_operation in commands:
                result = self._run(unit, unit_operation)
                results.append(result)
                if result.returncode != 0:
                    break

        ok = len(results) == len(commands) and all(item.returncode == 0 for item in results)
        return {
            "ok": ok,
            "target": target,
            "operation": operation,
            "results": [item.as_dict() for item in results],
        }

    def _commands(self, target: str, operation: str) -> list[tuple[str, str]]:
        if target == "proxy":
            return [(SERVICE_UNITS["proxy"], operation)]
        if target == "ikev2":
            if operation == "start":
                return [
                    (SERVICE_UNITS["firewall"], "start"),
                    (SERVICE_UNITS["ikev2"], "start"),
                ]
            if operation == "restart":
                return [
                    (SERVICE_UNITS["firewall"], "start"),
                    (SERVICE_UNITS["ikev2"], "restart"),
                ]
            return [(SERVICE_UNITS["ikev2"], "stop")]

        if operation == "stop":
            return [
                (SERVICE_UNITS["proxy"], "stop"),
                (SERVICE_UNITS["ikev2"], "stop"),
                (SERVICE_UNITS["firewall"], "stop"),
            ]
        if operation == "start":
            return [
                (SERVICE_UNITS["firewall"], "start"),
                (SERVICE_UNITS["ikev2"], "start"),
                (SERVICE_UNITS["proxy"], "start"),
            ]
        return [
            (SERVICE_UNITS["firewall"], "start"),
            (SERVICE_UNITS["ikev2"], "restart"),
            (SERVICE_UNITS["proxy"], "restart"),
        ]

    def _run(self, unit: str, operation: str) -> CommandResult:
        try:
            completed = self._runner(
                [self._systemctl, operation, unit],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            return CommandResult(unit, operation, 124, "systemctl timed out")
        except OSError:
            return CommandResult(unit, operation, 127, "systemctl could not be executed")
        detail = (completed.stderr or completed.stdout).strip()
        if completed.returncode == 0:
            detail = "completed"
        elif not detail:
            detail = "systemctl failed"
        return CommandResult(unit, operation, completed.returncode, detail[:1000])


class AuditLogger:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def write(
        self,
        *,
        peer_uid: int,
        target: str,
        operation: str,
        stage: str,
        ok: bool | None,
        action: str = "service_control",
    ) -> None:
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "peer_uid": peer_uid,
            "action": action,
            "target": target,
            "operation": operation,
            "stage": stage,
            "ok": ok,
        }
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        with self._lock:
            descriptor = os.open(self.path, flags, 0o600)
            try:
                opened = os.fstat(descriptor)
                if not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.geteuid():
                    raise PermissionError("Audit log must be a regular file owned by the broker user.")
                os.fchmod(descriptor, 0o600)
                os.write(descriptor, (json.dumps(event, separators=(",", ":")) + "\n").encode())
            finally:
                os.close(descriptor)


class BrokerRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        _peer_pid, peer_uid, _peer_gid = self.server.peer_credentials(self.request)  # type: ignore[attr-defined]
        if peer_uid != self.server.allowed_uid:  # type: ignore[attr-defined]
            self._reply({"ok": False, "error": "peer_not_allowed"})
            return

        raw = self.rfile.readline(MAX_MESSAGE_BYTES + 1)
        if not raw or len(raw) > MAX_MESSAGE_BYTES or not raw.endswith(b"\n"):
            self._reply({"ok": False, "error": "invalid_message"})
            return
        try:
            request = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._reply({"ok": False, "error": "invalid_json"})
            return
        if not isinstance(request, dict) or request.get("version") != PROTOCOL_VERSION:
            self._reply({"ok": False, "error": "unsupported_protocol"})
            return

        action = request.get("action")
        if action == "ping":
            installed = installation_state()
            self._reply({"ok": True, "version": PROTOCOL_VERSION, "installed": installed,
                         "user_management": installed is True, "session_monitoring": installed is True, "diagnostics": True})
            return
        if action == "diagnostics":
            if set(request) != {"version", "action"}:
                self._reply({"ok": False, "status": 400, "message": "Diagnostics accepts no command arguments."})
                return
            self._reply(self.server.diagnostics.snapshot())
            return
        if action == "sessions_list":
            if set(request) - {"version", "action", "search", "page"}:
                self._reply({"ok": False, "status": 400, "message": "Unsupported session fields."})
                return
            self._reply(self.server.sessions.list_sessions(request.get("search", ""), request.get("page", 0)))
            return
        if action in {"users_list", "users_manage"}:
            self._users(request, peer_uid)
            return
        if action != "service_control":
            self._reply({"ok": False, "error": "unsupported_action"})
            return

        target = request.get("target")
        operation = request.get("operation")
        if not isinstance(target, str) or not isinstance(operation, str):
            self._reply({"ok": False, "error": "invalid_action"})
            return
        try:
            self.server.audit.write(  # type: ignore[attr-defined]
                peer_uid=peer_uid,
                target=target,
                operation=operation,
                stage="requested",
                ok=None,
            )
        except OSError:
            self._reply({"ok": False, "error": "audit_failed"})
            return
        try:
            response = self.server.controller.execute(target, operation)  # type: ignore[attr-defined]
        except BrokerError as error:
            self._reply({"ok": False, "error": "invalid_action", "message": str(error)})
            return
        try:
            self.server.audit.write(  # type: ignore[attr-defined]
                peer_uid=peer_uid,
                target=target,
                operation=operation,
                stage="completed",
                ok=bool(response["ok"]),
            )
        except OSError:
            self._reply({"ok": False, "error": "audit_failed"})
            return
        self._reply(response)

    def _users(self, request: dict, peer_uid: int) -> None:
        action = request["action"]
        fields = None
        try:
            if action == "users_list":
                self._reply(self.server.users.list_users(request.get("search", ""), request.get("page", 0)))
                return
            # Validate before auditing, so hostile input cannot be logged as an identity.
            validate_change(request)
            fields = {"peer_uid": peer_uid, "target": request["username"],
                      "operation": request["operation"], "action": "user_management"}
            self.server.audit.write(**fields, stage="requested", ok=None)
            try:
                response = self.server.users.execute(request)
            except UserError as error:
                response = error.result()
            self.server.audit.write(**fields, stage="completed", ok=response.get("ok") is True)
            self._reply(response)
        except UserError as error:
            self._reply(error.result())
        except OSError:
            self._reply({"ok": False, "error": "users_unavailable", "status": 503,
                         "message": "VPN account storage or audit logging is unavailable."})

    def _reply(self, payload: dict[str, object]) -> None:
        self.wfile.write((json.dumps(payload, separators=(",", ":")) + "\n").encode())


class BrokerServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(
        self,
        socket_path: Path,
        allowed_uid: int,
        socket_gid: int,
        controller: ServiceController,
        audit: AuditLogger,
        users: UserManager | None = None,
        sessions: SessionReader | None = None,
        diagnostics: DiagnosticsReader | None = None,
    ):
        self.socket_path = socket_path
        self.allowed_uid = allowed_uid
        self.controller = controller
        self.audit = audit
        self.users = users or UserManager()
        self.sessions = sessions or SessionReader()
        self.diagnostics = diagnostics or DiagnosticsReader()
        self._prepare_socket_path(socket_gid)
        super().__init__(str(socket_path), BrokerRequestHandler)
        os.chown(socket_path, 0 if os.geteuid() == 0 else os.geteuid(), socket_gid)
        os.chmod(socket_path, 0o660)

    @staticmethod
    def peer_credentials(connection: socket.socket) -> tuple[int, int, int]:
        raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        return struct.unpack("3i", raw)

    def server_close(self) -> None:
        super().server_close()
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass

    def _prepare_socket_path(self, socket_gid: int) -> None:
        parent = self.socket_path.parent
        parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        parent_stat = parent.lstat()
        if stat.S_ISLNK(parent_stat.st_mode) or parent_stat.st_uid != os.geteuid():
            raise BrokerError("Broker socket directory must be owned by the broker user and not be a symlink.")
        os.chown(parent, os.geteuid(), socket_gid)
        os.chmod(parent, 0o750)
        try:
            existing = self.socket_path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(existing.st_mode) or existing.st_uid != os.geteuid():
            raise BrokerError("Refusing to replace an unsafe broker socket path.")
        self.socket_path.unlink()


class BrokerClient:
    def __init__(self, socket_path: Path = DEFAULT_SOCKET, timeout: float = 5.0):
        self.socket_path = socket_path
        self.timeout = timeout

    def request(self, payload: dict[str, object], timeout: float | None = None) -> dict[str, object]:
        message = json.dumps({"version": PROTOCOL_VERSION, **payload}, separators=(",", ":")).encode() + b"\n"
        if len(message) > MAX_MESSAGE_BYTES:
            raise BrokerError("Broker request is too large.")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(self.timeout if timeout is None else timeout)
                connection.connect(str(self.socket_path))
                connection.sendall(message)
                reader = connection.makefile("rb")
                raw = reader.readline(MAX_RESPONSE_BYTES + 1)
        except (OSError, TimeoutError) as error:
            raise BrokerError("Privileged broker is unavailable.") from error
        if not raw or len(raw) > MAX_RESPONSE_BYTES or not raw.endswith(b"\n"):
            raise BrokerError("Privileged broker returned an invalid response.")
        try:
            response = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise BrokerError("Privileged broker returned invalid JSON.") from error
        if not isinstance(response, dict):
            raise BrokerError("Privileged broker returned an invalid payload.")
        return response

    def available(self) -> bool:
        try:
            return self.status().get("ok") is True
        except BrokerError:
            return False

    def status(self) -> dict[str, object]:
        return self.request({"action": "ping"})

    def service_control(self, target: str, operation: str) -> dict[str, object]:
        return self.request({"action": "service_control", "target": target, "operation": operation})

    def list_users(self, search: str = "", page: int = 0) -> dict:
        return self.request({"action": "users_list", "search": search, "page": page})

    def diagnostics(self) -> dict:
        return self.request({"action": "diagnostics"}, timeout=25)

    def list_sessions(self, search: str = "", page: int = 0) -> dict:
        return self.request({"action": "sessions_list", "search": search, "page": page}, timeout=10)

    def manage_user(self, payload: dict) -> dict:
        return self.request({**payload, "action": "users_manage"}, timeout=45)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Privileged IKEv2 Web UI broker")
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET)
    parser.add_argument("--allowed-user", default="ikev2-web")
    parser.add_argument("--socket-group", default="ikev2-web")
    parser.add_argument("--audit-log", type=Path, default=DEFAULT_AUDIT_LOG)
    return parser.parse_args()


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit("The privileged broker must run as root.")
    args = parse_args()
    try:
        allowed_uid = pwd.getpwnam(args.allowed_user).pw_uid
        socket_gid = grp.getgrnam(args.socket_group).gr_gid
    except KeyError as error:
        raise SystemExit(f"Unknown broker user or group: {error}") from error
    server = BrokerServer(
        args.socket,
        allowed_uid,
        socket_gid,
        ServiceController(),
        AuditLogger(args.audit_log),
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
