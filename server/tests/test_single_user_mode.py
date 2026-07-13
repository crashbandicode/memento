from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from server.config import settings  # noqa: E402
from server.middleware.auth import is_single_user_allowed  # noqa: E402


class SingleUserModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._previous = settings.single_user_mode

    def tearDown(self) -> None:
        settings.single_user_mode = self._previous

    def test_mode_limits_access_to_owner_and_admin(self) -> None:
        settings.single_user_mode = True

        self.assertTrue(is_single_user_allowed(SimpleNamespace(role="owner")))
        self.assertTrue(is_single_user_allowed(SimpleNamespace(role="admin")))
        self.assertFalse(is_single_user_allowed(SimpleNamespace(role="viewer")))
        self.assertFalse(is_single_user_allowed(None))

    def test_mode_disabled_keeps_normal_multi_user_behavior(self) -> None:
        settings.single_user_mode = False

        self.assertTrue(is_single_user_allowed(SimpleNamespace(role="viewer")))
        self.assertTrue(is_single_user_allowed(None))


if __name__ == "__main__":
    unittest.main()
