"""Password, session, CSRF, and login throttling primitives."""

from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import stat
import tempfile
import threading
import time
from typing import Callable

from webui.totp import matching_counter, new_secret, provisioning_uri, valid_secret


SCRYPT_N = 1 << 14
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
SESSION_TOKEN_BYTES = 32
CSRF_TOKEN_BYTES = 32
DEFAULT_SESSION_TTL = 3600
MAX_SESSION_TTL = 86400


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str, salt: bytes | None = None) -> str:
    if len(password) < 12:
        raise ValueError("Password must contain at least 12 characters.")
    salt = salt or secrets.token_bytes(SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_encode(salt)}${_encode(derived)}"


def verify_password(password: str, encoded_hash: str) -> bool:
    try:
        algorithm, n_text, r_text, p_text, salt_text, expected_text = encoded_hash.split("$")
        if algorithm != "scrypt":
            return False
        n, r, p = int(n_text), int(r_text), int(p_text)
        if n != SCRYPT_N or r != SCRYPT_R or p != SCRYPT_P:
            return False
        salt = _decode(salt_text)
        expected = _decode(expected_text)
        if len(salt) != SALT_BYTES or len(expected) != 32:
            return False
        actual = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


@dataclass(frozen=True)
class AuthConfig:
    username: str
    password_hash: str
    secure_cookie: bool = True
    session_ttl_seconds: int = DEFAULT_SESSION_TTL
    totp_secret: str | None = None
    totp_last_counter: int = -1

    @classmethod
    def from_file(cls, path: Path) -> "AuthConfig":
        try:
            if path.is_symlink():
                raise ValueError("Web UI config must not be a symlink.")
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError as error:
            raise ValueError(f"Cannot read Web UI config: {path}") from error
        if mode & 0o077:
            raise ValueError(f"Web UI config must not be accessible by group or others: {path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"Invalid Web UI config: {path}") from error

        if not isinstance(payload, dict):
            raise ValueError("Web UI config must be a JSON object.")
        username = payload.get("username")
        password_hash = payload.get("password_hash")
        secure_cookie = payload.get("secure_cookie", True)
        session_ttl = payload.get("session_ttl_seconds", DEFAULT_SESSION_TTL)
        if not isinstance(username, str) or not username or len(username) > 64:
            raise ValueError("Config username must contain between 1 and 64 characters.")
        if not isinstance(password_hash, str) or not password_hash.startswith("scrypt$"):
            raise ValueError("Config password_hash is missing or unsupported.")
        if not isinstance(secure_cookie, bool):
            raise ValueError("Config secure_cookie must be true or false.")
        if not isinstance(session_ttl, int) or not 300 <= session_ttl <= MAX_SESSION_TTL:
            raise ValueError("Config session_ttl_seconds must be between 300 and 86400.")
        totp_secret = payload.get("totp_secret")
        totp_last_counter = payload.get("totp_last_counter", -1)
        if totp_secret is not None and not valid_secret(totp_secret):
            raise ValueError("Invalid two-factor authentication secret.")
        if type(totp_last_counter) is not int or totp_last_counter < -1:
            raise ValueError("Invalid two-factor authentication counter.")
        return cls(username, password_hash, secure_cookie, session_ttl, totp_secret, totp_last_counter)


@contextmanager
def locked_config(path: Path):
    descriptor = os.open(path.parent / f".{path.name}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        owner = path.stat()
        if os.geteuid() == 0:
            os.fchown(descriptor, owner.st_uid, owner.st_gid)
        os.fchmod(descriptor, 0o600)
        yield
    finally:
        os.close(descriptor)


def write_existing_config(path: Path, payload: dict) -> None:
    """Replace private configuration atomically while retaining its owner."""
    original = path.stat()
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as config_file:
            os.fchown(config_file.fileno(), original.st_uid, original.st_gid)
            os.fchmod(config_file.fileno(), 0o600)
            json.dump(payload, config_file, indent=2)
            config_file.write("\n")
            config_file.flush()
            os.fsync(config_file.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class AuthError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass
class Session:
    username: str
    csrf_token: str
    expires_at: float


class AuthManager:
    def __init__(
        self,
        config: AuthConfig,
        clock: Callable[[], float] = time.monotonic,
        max_failures: int = 5,
        failure_window: int = 300,
        config_path: Path | None = None,
        wall_clock: Callable[[], float] = time.time,
    ):
        self.config = config
        self._clock = clock
        self._wall_clock = wall_clock
        self._config_path = config_path
        self._pending_totp: dict[str, tuple[str, float]] = {}
        self._max_failures = max_failures
        self._failure_window = failure_window
        self._sessions: dict[str, Session] = {}
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.RLock()

    @contextmanager
    def _state(self):
        with self._lock:
            if self._config_path is None:
                yield
                return
            try:
                with locked_config(self._config_path):
                    current = AuthConfig.from_file(self._config_path)
                    if (current.username, current.password_hash, current.totp_secret) != (
                        self.config.username, self.config.password_hash, self.config.totp_secret
                    ):
                        self._sessions.clear()
                        self._pending_totp.clear()
                    self.config = current
                    yield
            except (OSError, ValueError) as error:
                raise AuthError("Authentication settings are unavailable. Check the server configuration.", 503) from error

    def _save(self, **updates) -> None:
        if self._config_path is None:
            raise AuthError("Two-factor settings require a persistent authentication config.", 503)
        payload = json.loads(self._config_path.read_text(encoding="utf-8"))
        payload.update(updates)
        write_existing_config(self._config_path, payload)
        self.config = AuthConfig.from_file(self._config_path)

    def _password_valid(self, username: str, password: str) -> bool:
        username_ok = hmac.compare_digest(username.encode("utf-8"), self.config.username.encode("utf-8"))
        return verify_password(password, self.config.password_hash) and username_ok

    def authenticate(self, username: str, password: str, client_key: str, code: object = None) -> tuple[str, Session] | None:
        with self._state():
            if not self.login_allowed(client_key)[0]:
                return None
            if not self._password_valid(username, password):
                self.record_failure(client_key)
                return None
            if self.config.totp_secret:
                counter = matching_counter(self.config.totp_secret, code, self._wall_clock(), self.config.totp_last_counter)
                if counter is None:
                    self.record_failure(client_key)
                    return None
                # Persist consumption before issuing a session, including across restarts.
                self._save(totp_last_counter=counter)
            self._failures.pop(client_key, None)
            self._remove_expired_sessions()
            token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
            session = Session(
                username=self.config.username,
                csrf_token=secrets.token_urlsafe(CSRF_TOKEN_BYTES),
                expires_at=self._clock() + self.config.session_ttl_seconds,
            )
            self._sessions[token] = session
            return token, session

    def two_factor_status(self) -> dict:
        with self._state():
            return {"enabled": bool(self.config.totp_secret)}

    def _security_session(self, token: str | None, client_key: str) -> Session:
        self._remove_expired_sessions()
        session = self._sessions.get(token)
        if session is None:
            raise AuthError("Sign in to continue.", 401)
        if not self.login_allowed(client_key)[0]:
            raise AuthError("Too many failed attempts. Wait a few minutes and try again.", 429)
        return session

    def _check_password(self, password: object, client_key: str) -> None:
        if not isinstance(password, str) or not self._password_valid(self.config.username, password):
            self.record_failure(client_key)
            raise AuthError("Invalid password or authenticator code.", 403)

    def begin_two_factor(self, token: str, password: object, client_key: str) -> dict:
        with self._state():
            self._security_session(token, client_key)
            if self.config.totp_secret:
                raise AuthError("Two-factor authentication is already enabled.", 409)
            self._check_password(password, client_key)
            secret = new_secret()
            self._pending_totp[token] = (secret, self._clock() + 600)
            return {"secret": secret, "uri": provisioning_uri(secret, self.config.username), "expires_in": 600}

    def confirm_two_factor(self, token: str, code: object, client_key: str) -> dict:
        with self._state():
            session = self._security_session(token, client_key)
            pending = self._pending_totp.get(token)
            if self.config.totp_secret or not pending or pending[1] <= self._clock():
                self._pending_totp.pop(token, None)
                raise AuthError("Setup expired or changed. Start two-factor setup again.", 409)
            counter = matching_counter(pending[0], code, self._wall_clock())
            if counter is None:
                self.record_failure(client_key)
                raise AuthError("Invalid authenticator code. Check the phone's time and try again.", 403)
            self._save(totp_secret=pending[0], totp_last_counter=counter)
            self._sessions = {token: session}
            self._pending_totp.clear()
            return {"enabled": True}

    def disable_two_factor(self, token: str, password: object, code: object, client_key: str) -> dict:
        with self._state():
            session = self._security_session(token, client_key)
            if not self.config.totp_secret:
                raise AuthError("Two-factor authentication is already disabled.", 409)
            self._check_password(password, client_key)
            counter = matching_counter(self.config.totp_secret, code, self._wall_clock(), self.config.totp_last_counter)
            if counter is None:
                self.record_failure(client_key)
                raise AuthError("Invalid or already used authenticator code. Wait for the next code.", 403)
            self._save(totp_secret=None, totp_last_counter=-1)
            self._sessions = {token: session}
            self._pending_totp.clear()
            return {"enabled": False}

    def cancel_two_factor(self, token: str) -> dict:
        with self._state():
            self._pending_totp.pop(token, None)
            return {"cancelled": True}

    def login_allowed(self, client_key: str) -> tuple[bool, int]:
        now = self._clock()
        with self._lock:
            recent = [stamp for stamp in self._failures.get(client_key, []) if now - stamp < self._failure_window]
            self._failures[client_key] = recent
        if len(recent) < self._max_failures:
            return True, 0
        retry_after = max(1, int(self._failure_window - (now - recent[0])))
        return False, retry_after

    def record_failure(self, client_key: str) -> None:
        with self._lock:
            self._failures.setdefault(client_key, []).append(self._clock())

    def session(self, token: str | None) -> Session | None:
        if not token:
            return None
        with self._state():
            self._remove_expired_sessions()
            return self._sessions.get(token)

    def destroy_session(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._sessions.pop(token, None)
            self._pending_totp.pop(token, None)

    def csrf_valid(self, session: Session, supplied_token: str | None) -> bool:
        return bool(supplied_token) and hmac.compare_digest(
            session.csrf_token.encode("utf-8"), supplied_token.encode("utf-8")
        )

    def _remove_expired_sessions(self) -> None:
        now = self._clock()
        expired = [token for token, session in self._sessions.items() if session.expires_at <= now]
        for token in expired:
            self._sessions.pop(token, None)
        self._pending_totp = {token: pending for token, pending in self._pending_totp.items()
                              if token in self._sessions and pending[1] > now}
