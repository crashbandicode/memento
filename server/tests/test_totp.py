from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from server.services.totp import code_at, decrypt_secret, encrypt_secret, verify_code  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
