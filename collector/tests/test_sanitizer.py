from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from collector.sanitizer import sanitize_jsonl, sanitize_text  # noqa: E402


class SanitizerTests(unittest.TestCase):
    def test_sanitize_text_removes_terminal_sequences(self) -> None:
        result = sanitize_text("before \u001b[7mmatch\u001b[0m after")

        self.assertEqual(result.content, "before match after")
        self.assertEqual(result.redaction_count, 0)
        self.assertFalse(result.has_sensitive_content)

    def test_sanitize_jsonl_keeps_one_record_per_line(self) -> None:
        result = sanitize_jsonl(
            '{"type":"one","token":"secret-value"}\n'
            '{"type":"two","payload":{"value":2}}\n'
        )

        lines = result.content.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["token"], "[REDACTED]")
        self.assertEqual(json.loads(lines[1])["type"], "two")


if __name__ == "__main__":
    unittest.main()
