"""Password, session, CSRF, and login throttling primitives."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import stat
import threading
import time
from typing import Callable


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

    @classmethod
    def from_file(cls, path: Path) -> "AuthConfig":
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError as error:
            raise ValueError(f"Cannot read Web UI config: {path}") from error
        if mode & 0o077:
            raise ValueError(f"Web UI config must not be accessible by group or others: {path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"Invalid Web UI config: {path}") from error

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
        return cls(username, password_hash, secure_cookie, session_ttl)


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
    ):
        self.config = config
        self._clock = clock
        self._max_failures = max_failures
        self._failure_window = failure_window
        self._sessions: dict[str, Session] = {}
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def authenticate(self, username: str, password: str, client_key: str) -> tuple[str, Session] | None:
        if not self.login_allowed(client_key)[0]:
            return None
        username_ok = hmac.compare_digest(username, self.config.username)
        password_ok = verify_password(password, self.config.password_hash)
        if not (username_ok and password_ok):
            self.record_failure(client_key)
            return None

        with self._lock:
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
        with self._lock:
            self._remove_expired_sessions()
            return self._sessions.get(token)

    def destroy_session(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._sessions.pop(token, None)

    def csrf_valid(self, session: Session, supplied_token: str | None) -> bool:
        return bool(supplied_token) and hmac.compare_digest(session.csrf_token, supplied_token)

    def _remove_expired_sessions(self) -> None:
        now = self._clock()
        expired = [token for token, session in self._sessions.items() if session.expires_at <= now]
        for token in expired:
            self._sessions.pop(token, None)
