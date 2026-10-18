"""RFC 4226/6238 TOTP, compatible with six-digit Authenticator accounts."""

import base64
import hashlib
import hmac
import re
import secrets
from urllib.parse import quote, urlencode

ISSUER = "IKEv2 Web UI"
PERIOD = 30


def valid_secret(secret: object) -> bool:
    return isinstance(secret, str) and re.fullmatch(r"[A-Z2-7]{32}", secret) is not None


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii")


def code_at(secret: str, counter: int) -> str:
    digest = hmac.new(base64.b32decode(secret), counter.to_bytes(8, "big"), hashlib.sha1).digest()
    offset = digest[-1] & 15
    value = int.from_bytes(digest[offset:offset + 4], "big") & 0x7fffffff
    return f"{value % 1000000:06d}"


def matching_counter(secret: str, code: object, now: float, last_counter: int = -1) -> int | None:
    if not isinstance(code, str) or re.fullmatch(r"[0-9]{6}", code) is None:
        return None
    current = int(now // PERIOD)
    # Allow one step of clock skew, but never accept a used or older step.
    for counter in (current, current - 1, current + 1):
        if counter >= 0 and counter > last_counter and hmac.compare_digest(code_at(secret, counter), code):
            return counter
    return None


def provisioning_uri(secret: str, username: str) -> str:
    label = quote(f"{ISSUER}:{username}", safe="")
    query = urlencode({"secret": secret, "issuer": ISSUER, "algorithm": "SHA1", "digits": 6, "period": PERIOD})
    return f"otpauth://totp/{label}?{query}"
