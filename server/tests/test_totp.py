from __future__ import annotations

import sys
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from fastapi import HTTPException  # noqa: E402

from server.api.auth import (  # noqa: E402
    LoginRequest,
    TotpConfirmRequest,
    TotpPasswordRequest,
    confirm_totp,
    disable_totp,
    login,
    setup_totp,
)
from server.db.models import User  # noqa: E402
from server.middleware.auth import hash_password  # noqa: E402
from server.services.totp import code_at, decrypt_secret, encrypt_secret, verify_code  # noqa: E402


class _ScalarResult:
    def __init__(self, value: object) -> None:
        self.value = value

    def scalar_one_or_none(self) -> object:
        return self.value


class _Session:
    def __init__(self, user: User) -> None:
        self.user = user
        self.flushes = 0

    async def execute(self, _statement: object) -> _ScalarResult:
        return _ScalarResult(self.user)

    async def flush(self) -> None:
        self.flushes += 1


class TotpTests(unittest.TestCase):
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"

    def test_rfc6238_six_digit_value(self) -> None:
        self.assertEqual(code_at(self.secret, 59), "287082")

    def test_encrypted_secret_verifies_current_window(self) -> None:
        encrypted = encrypt_secret(self.secret)
        self.assertNotIn(self.secret, encrypted)
        self.assertEqual(decrypt_secret(encrypted), self.secret)
        # Use the current wall-clock code because verification intentionally
        # accepts a one-step clock skew window.
        self.assertTrue(verify_code(encrypted, code_at(self.secret)))


class TotpAuthFlowTests(unittest.IsolatedAsyncioTestCase):
    password = "correct horse battery staple"

    def setUp(self) -> None:
        self.user = User(
            id=uuid.uuid4(),
            email="totp-regression@memento.test",
            name="TOTP Regression",
            hashed_password=hash_password(self.password),
            role="owner",
            status="active",
            collector_token="collector-token",
            totp_secret=None,
            totp_enabled=False,
        )
        self.db = _Session(self.user)

    async def test_wrong_reauthentication_password_is_not_a_bearer_failure(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            await setup_totp(
                TotpPasswordRequest(password="wrong password"),
                self.user,
                self.db,  # type: ignore[arg-type]
            )

        self.assertEqual(raised.exception.status_code, 403)
        self.assertIsNone(self.user.totp_secret)
        self.assertFalse(self.user.totp_enabled)
        self.assertEqual(self.db.flushes, 0)

    async def test_setup_confirm_login_and_disable_round_trip(self) -> None:
        setup = await setup_totp(
            TotpPasswordRequest(password=self.password),
            self.user,
            self.db,  # type: ignore[arg-type]
        )
        self.assertIn("otpauth://totp/", setup.provisioning_uri)
        self.assertNotEqual(self.user.totp_secret, setup.secret)
        self.assertFalse(self.user.totp_enabled)

        code = code_at(setup.secret)
        confirmed = await confirm_totp(
            TotpConfirmRequest(password=self.password, code=code),
            self.user,
            self.db,  # type: ignore[arg-type]
        )
        self.assertTrue(confirmed.totp_enabled)
        self.assertTrue(self.user.totp_enabled)

        with self.assertRaises(HTTPException) as missing_code:
            await login(LoginRequest(email=self.user.email, password=self.password), self.db)  # type: ignore[arg-type]
        self.assertEqual(missing_code.exception.status_code, 401)

        token = await login(
            LoginRequest(email=self.user.email, password=self.password, totp_code=code),
            self.db,  # type: ignore[arg-type]
        )
        self.assertEqual(token.user_id, str(self.user.id))

        disabled = await disable_totp(
            TotpConfirmRequest(password=self.password, code=code),
            self.user,
            self.db,  # type: ignore[arg-type]
        )
        self.assertFalse(disabled.totp_enabled)
        self.assertFalse(self.user.totp_enabled)
        self.assertIsNone(self.user.totp_secret)


if __name__ == "__main__":
    unittest.main()
