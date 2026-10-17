"""Fixed-path VPN user operations; never return or log stored EAP secrets."""

from __future__ import annotations

import fcntl
from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import tempfile
import threading

SECRETS_FILE = Path("/etc/ipsec.secrets")
STATE_FILE = Path("/var/lib/ikev2-easy-installer/state.env")
REQUEST_DIR = Path("/run/ikev2-web/users")
APPLY_SERVICE = "ikev2-web-users-apply.service"
MAX_FILE_BYTES = 2 * 1024 * 1024
USERNAME = re.compile(r"[A-Za-z0-9._@-]{1,64}\Z")
ENTRY = re.compile(r'^([A-Za-z0-9._@-]{1,64})[ \t]+:[ \t]+EAP[ \t]+"[^"\r\n]*"[ \t]*(?:#.*)?$')


class UserError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.status = code, status

    def result(self) -> dict:
        return {"ok": False, "error": self.code, "message": str(self), "status": self.status}


def validate_change(request: dict) -> None:
    if request.get("operation") not in {"create", "reset", "delete"}:
        raise UserError("invalid_operation", "Unsupported user operation.")
    if not isinstance(request.get("username"), str) or not USERNAME.fullmatch(request["username"]):
        raise UserError("invalid_username", "Use 1–64 letters, digits, dot, underscore, @ or hyphen.")
    if type(request.get("allow_last", False)) is not bool:
        raise UserError("invalid_confirmation", "Invalid last-user confirmation.")
    if request["operation"] != "delete":
        password = request.get("password")
        if not isinstance(password, str) or not re.fullmatch(r"[\x20-\x7e]{12,128}", password) or '"' in password:
            raise UserError("invalid_password", "Use 12–128 printable ASCII characters, excluding double quotes.")


def read_private(path: Path, maximum: int = MAX_FILE_BYTES) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_nlink != 1:
            raise UserError("unsafe_file", "VPN account storage must be private and owned by the service administrator.", 503)
        raw = source.read(maximum + 1)
        if len(raw) > maximum:
            raise UserError("storage_limit", "VPN account storage exceeds the supported size.", 503)
        return raw


def parse_users(raw: bytes) -> tuple[list[str], dict[str, int]]:
    try:
        lines = raw.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError as error:
        raise UserError("unsupported_storage", "Account storage is not supported by this panel.", 503) from error
    users = {}
    for index, line in enumerate(lines):
        text = line.rstrip("\r\n")
        if not text.strip() or text.lstrip().startswith("#"):
            continue
        entry = ENTRY.fullmatch(text)
        if entry:
            if entry[1] in users:
                raise UserError("duplicate_storage", "Duplicate VPN account entries require manual review.", 503)
            users[entry[1]] = index
        elif re.match(r"\s*include\b", text, re.I) or re.search(r":\s*EAP\b", text, re.I):
            raise UserError("unsupported_storage", "Included or nonstandard EAP entries require management through the installer.", 503)
    return lines, users


def atomic_private(path: Path, raw: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.webui-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class UserStore:
    def __init__(self, path: Path = SECRETS_FILE, state: Path = STATE_FILE, runner=subprocess.run):
        self.path, self.state, self.runner = path, state, runner

    def snapshot(self) -> bytes:
        if not self.state.is_file():
            raise UserError("not_installed", "A managed VPN installation is required.", 503)
        return read_private(self.path)

    def apply(self, request: dict) -> dict:
        validate_change(request)
        original = self.snapshot()
        if hashlib.sha256(original).hexdigest() != request.get("expected_digest"):
            raise UserError("conflict", "Accounts changed. Refresh the list before trying again.", 409)
        lines, users = parse_users(original)
        username, operation = request["username"], request["operation"]
        if operation == "create" and username in users:
            raise UserError("already_exists", "This VPN username already exists.", 409)
        if operation != "create" and username not in users:
            raise UserError("not_found", "This VPN user no longer exists. Refresh the list.", 404)
        if operation == "delete" and len(users) == 1 and request.get("allow_last") is not True:
            raise UserError("last_user", "Explicit confirmation is required to remove the last VPN account.", 409)
        if operation == "delete":
            del lines[users[username]]
        else:
            # stroke's quoted-secret parser is literal, not a backslash unescaper.
            password = request["password"]
            entry = f'{username} : EAP "{password}"\n'
            if operation == "reset":
                lines[users[username]] = entry
            else:
                if lines and not lines[-1].endswith("\n"):
                    lines[-1] += "\n"
                lines.append(entry)
        updated = "".join(lines).encode("utf-8")
        if len(updated) > MAX_FILE_BYTES:
            raise UserError("storage_limit", "VPN account storage exceeds the supported size.", 409)
        # Check again immediately before replacing, including edits made by the CLI.
        if self.snapshot() != original:
            raise UserError("conflict", "Accounts changed. Refresh the list before trying again.", 409)
        atomic_private(self.path, updated)
        try:
            active = self.runner(["/usr/bin/systemctl", "is-active", "--quiet", "strongswan-starter.service"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False)
            if active.returncode not in (0, 3):
                raise OSError("Service state unavailable")
            if active.returncode == 0:
                result = self.runner(["/usr/sbin/ipsec", "rereadsecrets"], stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, timeout=10, check=False)
                if result.returncode != 0:
                    raise OSError("Secrets reload failed")
        except (OSError, subprocess.TimeoutExpired) as error:
            if self.snapshot() == updated:
                atomic_private(self.path, original)
                try:
                    self.runner(["/usr/sbin/ipsec", "rereadsecrets"], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=10, check=False)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                raise UserError("reload_failed", "The original account file was restored. Runtime reload could not be confirmed.", 503) from error
            raise UserError("conflict", "An external account edit occurred. Refresh and check the server before retrying.", 409) from error
        return {"ok": True, "operation": operation, "username": username,
                "applies_on_start": active.returncode != 0}

    def check(self) -> dict:
        parse_users(self.snapshot())
        for command, allowed in [(["/usr/bin/systemctl", "is-active", "--quiet", "strongswan-starter.service"], {0, 3}),
                                 (["/usr/sbin/ipsec", "--version"], {0})]:
            result = self.runner(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False)
            if result.returncode not in allowed:
                raise UserError("helper_unavailable", "The account helper cannot access required VPN tools.", 503)
        return {"ok": True, "checked": True}


class UserManager:
    """The long-lived broker reads secrets; only a short-lived unit writes /etc."""

    def __init__(self, store: UserStore | None = None, directory: Path = REQUEST_DIR, runner=subprocess.run):
        self.store, self.directory, self.runner = store or UserStore(), directory, runner
        self._key = secrets.token_bytes(32)
        self._lock = threading.Lock()

    def revision(self, raw: bytes) -> str:
        # Do not expose an unsalted digest of password-bearing storage to browsers.
        return hmac.new(self._key, raw, hashlib.sha256).hexdigest()

    def list_users(self, search: str = "", page: int = 0) -> dict:
        if not isinstance(search, str) or len(search) > 64 or type(page) is not int or not 0 <= page <= 100000:
            raise UserError("invalid_query", "Invalid user search or page.")
        raw = self.store.snapshot()
        _, users = parse_users(raw)
        matching = sorted((name for name in users if search.casefold() in name.casefold()), key=str.casefold)
        pages = max(1, (len(matching) + 19) // 20)
        page = min(page, pages - 1)
        return {"ok": True, "users": [{"username": name} for name in matching[page * 20:(page + 1) * 20]],
                "total": len(users), "matched": len(matching), "page": page, "pages": pages,
                "revision": self.revision(raw)}

    def execute(self, request: dict) -> dict:
        validate_change(request)
        return self._execute(request)

    def check(self) -> dict:
        return self._execute({"operation": "check"})

    @contextmanager
    def _exclusive(self):
        if not self._lock.acquire(blocking=False):
            raise UserError("busy", "Another account change is running. Wait and refresh before retrying.", 409)
        try:
            yield
        finally:
            self._lock.release()

    def _execute(self, request: dict) -> dict:
        with self._exclusive():
            raw = self.store.snapshot()
            revision = request.get("revision")
            checking = request["operation"] == "check"
            if not checking and (not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{64}", revision) or not hmac.compare_digest(self.revision(raw), revision)):
                raise UserError("conflict", "Accounts changed. Refresh the list before trying again.", 409)
            self.directory.mkdir(mode=0o700, exist_ok=True)
            info = self.directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise UserError("unsafe_queue", "Private account operation storage is unavailable.", 503)
            payload = {key: request[key] for key in ("operation", "username", "password", "allow_last") if key in request}
            payload["expected_digest"] = hashlib.sha256(raw).hexdigest()
            payload["request_id"] = secrets.token_hex(16)
            request_path, result_path = self.directory / "request.json", self.directory / "result.json"
            result_path.unlink(missing_ok=True)
            try:
                atomic_private(request_path, json.dumps(payload).encode())
                result = self.runner(["/usr/bin/systemctl", "start", APPLY_SERVICE], stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, timeout=40, check=False)
                if result.returncode != 0:
                    raise UserError("apply_failed", "The account change was not confirmed. Refresh before retrying.", 503)
                response = json.loads(read_private(result_path, 4096))
                if not isinstance(response, dict):
                    raise ValueError("Invalid helper response")
                if response.get("request_id") != payload["request_id"]:
                    raise ValueError("Unmatched helper response")
                # Construct the public result explicitly; never relay arbitrary helper data.
                if response.get("ok") is True:
                    if checking:
                        return {"ok": True, "checked": response.get("checked") is True}
                    return {"ok": True, "username": request["username"], "operation": request["operation"],
                            "applies_on_start": response.get("applies_on_start") is True}
                return {"ok": False, "error": response.get("error", "apply_failed"),
                        "message": response.get("message", "Account update failed."), "status": response.get("status", 503)}
            except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                raise UserError("apply_failed", "The account change was not confirmed. Refresh before retrying.", 503) from error
            finally:
                # If the helper is still running after a timeout, its in-memory request
                # may complete; do not retry mutations automatically.
                request_path.unlink(missing_ok=True)
                result_path.unlink(missing_ok=True)


def apply_queued_request() -> None:
    if os.geteuid() != 0:
        raise SystemExit("The account helper must run as root.")
    directory_info = REQUEST_DIR.lstat()
    if not stat.S_ISDIR(directory_info.st_mode) or directory_info.st_uid != 0 or stat.S_IMODE(directory_info.st_mode) != 0o700:
        raise SystemExit("Private account queue is unavailable.")
    with open(REQUEST_DIR / "apply.lock", "a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        request = None
        try:
            request = json.loads(read_private(REQUEST_DIR / "request.json", 4096))
            if not isinstance(request, dict):
                raise ValueError("Invalid request")
            if request.get("operation") == "check":
                result = UserStore().check()
            else:
                result = UserStore().apply(request)
        except UserError as error:
            result = error.result()
        except (OSError, ValueError, subprocess.TimeoutExpired):
            result = UserError("apply_failed", "Account storage could not be updated safely.", 503).result()
        result["request_id"] = request.get("request_id") if isinstance(request, dict) else None
        atomic_private(REQUEST_DIR / "result.json", json.dumps(result).encode())


if __name__ == "__main__":
    apply_queued_request()
