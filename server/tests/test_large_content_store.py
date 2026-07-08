from __future__ import annotations

import hashlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from server.config import settings  # noqa: E402
from server.services.ingest_service import (  # noqa: E402
    MAX_DOCUMENT_METADATA_BYTES,
    MAX_STORED_MESSAGE_CHARS,
    _bounded_message_text,
    _history_line_number,
    _json_size,
    _is_externalized_delta_update,
    _prepare_document_metadata,
)
from server.services.large_content_store import (  # noqa: E402
    read_large_content_prefix,
    store_large_content,
)


class _FakeS3:
    def __init__(self, *, existing_bucket: bool = False, wrong_size: bool = False):
        self.existing_bucket = existing_bucket
        self.wrong_size = wrong_size
        self.created = []
        self.deleted = []
        self.uploads = []
        self.size = 0

    def head_bucket(self, *, Bucket):
        if not self.existing_bucket:
            raise ClientError(
                {"Error": {"Code": "NoSuchBucket", "Message": "missing"}},
                "HeadBucket",
            )
        return {"Bucket": Bucket}

    def create_bucket(self, *, Bucket):
        self.created.append(Bucket)
        self.existing_bucket = True

    def upload_file(self, path, bucket, key, ExtraArgs):
        self.size = Path(path).stat().st_size
        self.uploads.append((path, bucket, key, ExtraArgs))

    def head_object(self, *, Bucket, Key):
        return {"ContentLength": self.size + (1 if self.wrong_size else 0)}

    def delete_object(self, *, Bucket, Key):
        self.deleted.append((Bucket, Key))


class _PrefixS3:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.calls = []

    def get_object(self, *, Bucket, Key, Range):
        self.calls.append((Bucket, Key, Range))
        return {"Body": io.BytesIO(self.payload)}


class LargeContentStoreTests(unittest.TestCase):
    def test_prefix_read_uses_a_bounded_s3_range(self) -> None:
        client = _PrefixS3(b"abcdef")

        prefix = read_large_content_prefix(
            "raw/thread.txt",
            max_bytes=5,
            s3_client=client,
        )

        self.assertEqual(prefix, "abcde")
        self.assertEqual(
            client.calls,
            [(settings.s3_bucket, "raw/thread.txt", "bytes=0-4")],
        )

    def test_raw_payload_is_streamed_to_deterministic_private_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            payload = Path(temporary) / "payload.bin"
            payload.write_bytes(b"raw transcript\n")
            client = _FakeS3()

            key = store_large_content(
                payload,
                user_id="11111111-1111-1111-1111-111111111111",
                device_id="device/private/name",
                job_id="a" * 64,
                s3_client=client,
            )

        device_key = hashlib.sha256(b"device/private/name").hexdigest()
        self.assertEqual(
            key,
            f"raw/11111111-1111-1111-1111-111111111111/{device_key}/{'a' * 64}.txt",
        )
        self.assertEqual(client.created, [settings.s3_bucket])
        self.assertEqual(client.uploads[0][1], settings.s3_bucket)
        self.assertEqual(client.uploads[0][2], key)
        self.assertEqual(
            client.uploads[0][3]["ContentType"],
            "text/plain; charset=utf-8",
        )

    def test_size_mismatch_is_not_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            payload = Path(temporary) / "payload.bin"
            payload.write_bytes(b"raw transcript\n")
            with self.assertRaisesRegex(OSError, "size verification"):
                store_large_content(
                    payload,
                    user_id="user",
                    device_id="device",
                    job_id="b" * 64,
                    s3_client=_FakeS3(existing_bucket=True, wrong_size=True),
                )

    def test_oversized_single_message_is_bounded_with_marker(self) -> None:
        original = "x" * (MAX_STORED_MESSAGE_CHARS + 10)
        bounded = _bounded_message_text(original, MAX_STORED_MESSAGE_CHARS)

        self.assertTrue(bounded.startswith("x" * 100))
        self.assertIn("oversized message truncated", bounded)
        self.assertLessEqual(len(bounded.encode("utf-8")), MAX_STORED_MESSAGE_CHARS)

    def test_four_byte_unicode_is_bounded_by_encoded_size(self) -> None:
        bounded = _bounded_message_text("😀" * MAX_STORED_MESSAGE_CHARS, 1024)

        self.assertLessEqual(len(bounded.encode("utf-8")), 1024)
        self.assertIn("oversized message truncated", bounded)

    def test_small_varchar_limit_is_always_honored(self) -> None:
        bounded = _bounded_message_text("malformed-type" * 100, 50)

        self.assertLessEqual(len(bounded.encode("utf-8")), 50)

    def test_prompt_history_is_transient_and_document_metadata_is_bounded(self) -> None:
        metadata, history, first_prompt = _prepare_document_metadata(
            {
                "project_hash": "project",
                "user_history": [{"text": "hello", "ts": 42}],
                "first_user_message": "first",
                "oversized": "😀" * MAX_DOCUMENT_METADATA_BYTES,
            }
        )

        self.assertNotIn("user_history", metadata)
        self.assertNotIn("first_user_message", metadata)
        self.assertEqual(history, [{"text": "hello", "ts": 42}])
        self.assertEqual(first_prompt, "first")
        self.assertLessEqual(_json_size(metadata), MAX_DOCUMENT_METADATA_BYTES)

    def test_small_delta_preserves_externalized_full_snapshot(self) -> None:
        externalized = SimpleNamespace(content=None, content_s3_key="raw/job.txt")
        inline = SimpleNamespace(content="full", content_s3_key=None)

        self.assertTrue(
            _is_externalized_delta_update(
                externalized,
                mode="delta",
                persist_content=True,
            )
        )
        self.assertFalse(
            _is_externalized_delta_update(
                externalized,
                mode="full",
                persist_content=True,
            )
        )
        self.assertFalse(
            _is_externalized_delta_update(
                inline,
                mode="delta",
                persist_content=True,
            )
        )

    def test_injected_history_line_numbers_never_collide_with_parsed_rows(self) -> None:
        self.assertEqual(_history_line_number(0), -2_000)
        self.assertEqual(_history_line_number(1_999), -1)
        with self.assertRaises(ValueError):
            _history_line_number(2_000)


if __name__ == "__main__":
    unittest.main()
