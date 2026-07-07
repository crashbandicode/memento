from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from collector.sanitizer import sanitize_text  # noqa: E402


class SanitizerTests(unittest.TestCase):
    def test_sanitize_text_removes_terminal_sequences(self) -> None:
        result = sanitize_text("before \u001b[7mmatch\u001b[0m after")

        self.assertEqual(result.content, "before match after")
        self.assertEqual(result.redaction_count, 0)
        self.assertFalse(result.has_sensitive_content)


if __name__ == "__main__":
    unittest.main()
