"""Minimal encrypted-at-rest RFC 6238 TOTP helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from urllib.parse import quote

from cryptography.fernet import Fernet, InvalidToken

from ..config import settings


def _cipher() -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(settings.secret_key.encode()).digest())
    return Fernet(key)


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def encrypt_secret(secret: str) -> str:
    return _cipher().encrypt(secret.encode("ascii")).decode("ascii")


def decrypt_secret(encrypted: str) -> str | None:
    try:
        return _cipher().decrypt(encrypted.encode("ascii")).decode("ascii")
    except (InvalidToken, ValueError):
        return None


def code_at(secret: str, for_time: float | None = None) -> str:
    counter = int((time.time() if for_time is None else for_time) // 30)
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, counter.to_bytes(8, "big"), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = int.from_bytes(digest[offset:offset + 4], "big") & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


def verify_code(encrypted_secret: str | None, code: str | None) -> bool:
    if not encrypted_secret or not code or not code.isascii() or not code.isdigit() or len(code) != 6:
        return False
    secret = decrypt_secret(encrypted_secret)
    if not secret:
        return False
    now = time.time()
    return any(hmac.compare_digest(code, code_at(secret, now + step * 30)) for step in (-1, 0, 1))


def provisioning_uri(secret: str, email: str, issuer: str = "Memento") -> str:
    label = quote(f"{issuer}:{email}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}&algorithm=SHA1&digits=6&period=30"
