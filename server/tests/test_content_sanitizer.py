"""Focused tests for the bounded-memory transcript sanitizer."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from server.services.content_sanitizer import sanitize_content_file  # noqa: E402


class ContentSanitizerTests(unittest.TestCase):
    def test_redacts_tokens_across_every_chunk_boundary(self) -> None:
        api_token = b"sk-" + (b"A" * 93)
        github_token = b"ghp_" + (b"B" * 36)
        telegram_token = b"bot123456:" + (b"C" * 35)
        payload = (
            b'{"message":"'
            + api_token
            + b" "
            + github_token
            + b" "
            + telegram_token
            + b'"}\n'
        )
        expected = (
            b'{"message":"[API_KEY_REDACTED] [GITHUB_TOKEN_REDACTED] '
            b'[TELEGRAM_BOT_TOKEN_REDACTED]"}\n'
        )

        for chunk_size in (1, 2, 3, 7, 64):
            with (
                self.subTest(chunk_size=chunk_size),
                tempfile.TemporaryDirectory() as temp,
            ):
                directory = Path(temp)
                source = directory / "source.jsonl"
                target = directory / "sanitized.jsonl"
                source.write_bytes(payload)

                result = sanitize_content_file(
                    source,
                    target,
                    chunk_size=chunk_size,
                )
                output = target.read_bytes()

                self.assertEqual(result.path, target)
                self.assertEqual(result.size, len(output))
                self.assertTrue(result.had_sensitive)
                self.assertEqual(output, expected)
                self.assertNotIn(api_token, output)
                self.assertNotIn(github_token, output)
                self.assertNotIn(telegram_token, output)

    def test_removes_nul_without_reporting_a_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            source = directory / "source.jsonl"
            target = directory / "sanitized.jsonl"
            source.write_bytes(b'{"message":"before\x00after"}\n')

            result = sanitize_content_file(source, target, chunk_size=1)
            output = target.read_bytes()

            self.assertEqual(output, b'{"message":"beforeafter"}\n')
            self.assertEqual(result.size, len(output))
            self.assertFalse(result.had_sensitive)

    def test_redacts_pem_with_delimiters_and_body_across_lines(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            source = directory / "source.jsonl"
            target = directory / "sanitized.jsonl"
            source.write_bytes(
                b'{"before":true}\n'
                b"-----BEGIN\nOPENSSH PRIVATE KEY-----\n"
                b"not-a-real-private-key\nmore-placeholder-material\n"
                b"-----END\nOPENSSH PRIVATE KEY-----\n"
                b'{"after":true}\n'
            )

            result = sanitize_content_file(source, target, chunk_size=2)

            self.assertEqual(
                target.read_bytes(),
                b'{"before":true}\n[PRIVATE_KEY_REDACTED]\n{"after":true}\n',
            )
            self.assertTrue(result.had_sensitive)

    def test_failed_prefixes_are_preserved_at_boundaries(self) -> None:
        payload = (
            b"sk-too-short\n"
            + b"ghp_"
            + (b"G" * 35)
            + b"!\n"
            + b"bot42:"
            + (b"T" * 34)
            + b"!\n-----BEGIN not-a-private-key\n"
        )
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            source = directory / "source.jsonl"
            target = directory / "sanitized.jsonl"
            source.write_bytes(payload)

            result = sanitize_content_file(source, target, chunk_size=1)

            self.assertEqual(target.read_bytes(), payload)
            self.assertFalse(result.had_sensitive)

    def test_unbounded_telegram_bot_id_does_not_need_an_in_memory_line(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            source = directory / "source.jsonl"
            target = directory / "sanitized.jsonl"
            source.write_bytes(b"bot" + (b"9" * 200_000) + b":" + (b"X" * 35))

            result = sanitize_content_file(source, target, chunk_size=17)

            self.assertEqual(
                target.read_bytes(),
                b"[TELEGRAM_BOT_TOKEN_REDACTED]",
            )
            self.assertTrue(result.had_sensitive)

    def test_existing_output_is_replaced_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            source = directory / "source.jsonl"
            target = directory / "sanitized.jsonl"
            source.write_bytes(b'{"message":"safe"}\n')
            target.write_bytes(b"stale output that must not survive")

            result = sanitize_content_file(source, target, chunk_size=4)

            self.assertEqual(target.read_bytes(), source.read_bytes())
            self.assertEqual(result.size, target.stat().st_size)
            self.assertFalse(result.had_sensitive)
            self.assertEqual(list(directory.glob(f".{target.name}.*.tmp")), [])

    def test_source_can_be_replaced_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            transcript = Path(temp) / "transcript.jsonl"
            transcript.write_bytes(b"prefix sk-" + (b"Z" * 20) + b" suffix\x00")

            result = sanitize_content_file(transcript, transcript, chunk_size=3)

            self.assertEqual(
                transcript.read_bytes(),
                b"prefix [API_KEY_REDACTED] suffix",
            )
            self.assertEqual(result.path, transcript)
            self.assertTrue(result.had_sensitive)

    def test_rejects_non_positive_chunk_size(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            source = directory / "source.jsonl"
            source.write_bytes(b"safe")

            with self.assertRaisesRegex(ValueError, "chunk_size must be positive"):
                sanitize_content_file(source, directory / "out.jsonl", chunk_size=0)


if __name__ == "__main__":
    unittest.main()
