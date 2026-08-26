from __future__ import annotations

import asyncio
import hashlib
import io
import sys
import tempfile
import unittest
import uuid
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
    DocumentContentIntegrityError,
    DocumentContentUnavailableError,
    document_content,
    document_content_key,
    finalize_document_content,
    iter_large_content_lines,
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


class _StreamingBody(io.BytesIO):
    def iter_lines(self, *, chunk_size, keepends):
        self.chunk_size = chunk_size
        self.keepends = keepends
        yield from self.read().splitlines(keepends=keepends)


class _StreamingS3:
    def __init__(self, payload: bytes):
        self.body = _StreamingBody(payload)
        self.calls = []

    def get_object(self, *, Bucket, Key):
        self.calls.append((Bucket, Key))
        return {"Body": self.body}


class _ImmutableS3:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.put_calls = 0

    def get_object(self, *, Bucket, Key):
        try:
            payload = self.objects[Key]
        except KeyError as exc:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject") from exc
        return {"Body": io.BytesIO(payload), "ContentLength": len(payload)}

    def put_object(self, *, Bucket, Key, Body, **_kwargs):
        self.put_calls += 1
        self.objects[Key] = Body.read() if hasattr(Body, "read") else bytes(Body)


class LargeContentStoreTests(unittest.TestCase):
    def test_finalizer_puts_verifies_and_returns_transaction_pointer(self) -> None:
        client = _ImmutableS3()
        document_id = uuid.uuid4()

        pointer = asyncio.run(
            finalize_document_content(
                document_id=document_id,
                content="emoji 😀\n",
                s3_client=client,
            )
        )

        self.assertEqual(
            pointer.key,
            document_content_key(document_id=document_id, sha256=pointer.sha256),
        )
        self.assertEqual(client.objects[pointer.key], "emoji 😀\n".encode("utf-8"))
        self.assertEqual(pointer.size_bytes, len("emoji 😀\n".encode("utf-8")))
        self.assertIsNotNone(pointer.verified_at)

    def test_finalizer_reuses_existing_exact_immutable_object(self) -> None:
        client = _ImmutableS3()
        document_id = uuid.uuid4()
        first = asyncio.run(
            finalize_document_content(
                document_id=document_id,
                content="same bytes",
                s3_client=client,
            )
        )
        second = asyncio.run(
            finalize_document_content(
                document_id=document_id,
                content="same bytes",
                s3_client=client,
            )
        )

        self.assertEqual(first.key, second.key)
        self.assertEqual(client.put_calls, 1)

    def test_finalizer_refuses_mismatched_existing_content_addressed_key(self) -> None:
        client = _ImmutableS3()
        document_id = uuid.uuid4()
        expected_hash = hashlib.sha256(b"expected").hexdigest()
        client.objects[
            document_content_key(document_id=document_id, sha256=expected_hash)
        ] = b"corrupt"

        with self.assertRaises(DocumentContentIntegrityError):
            asyncio.run(
                finalize_document_content(
                    document_id=document_id,
                    content="expected",
                    s3_client=client,
                )
            )
        self.assertEqual(client.put_calls, 0)

    def test_verified_pointer_missing_object_raises_unavailable(self) -> None:
        document = SimpleNamespace(
            id=uuid.uuid4(),
            content_s3_key="document-content/v1/missing",
            content_object_sha256=hashlib.sha256(b"expected").hexdigest(),
            content_object_size_bytes=len(b"expected"),
            content_object_verified_at=object(),
        )
        with self.assertRaises(DocumentContentUnavailableError):
            asyncio.run(document_content(object(), document, s3_client=_ImmutableS3()))

    def test_large_content_lines_stream_without_whole_object_limit(self) -> None:
        client = _StreamingS3(b'{"first": 1}\n{"second": 2}\n')

        lines = list(iter_large_content_lines("raw/thread.txt", s3_client=client))

        self.assertEqual(lines, ['{"first": 1}', '{"second": 2}'])
        self.assertEqual(client.calls, [(settings.s3_bucket, "raw/thread.txt")])
        self.assertTrue(client.body.closed)

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
