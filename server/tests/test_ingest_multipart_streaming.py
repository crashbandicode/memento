from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from server.api.ingest import (  # noqa: E402
    IngestResponse,
    _UPLOAD_STREAM_CHUNK_BYTES,
    ingest_file_upload,
)
from server.services.conversation_stream import ConversationFileSource  # noqa: E402
from server.services.large_content_store import DATABASE_CONTENT_MAX_BYTES  # noqa: E402


class _BoundedUpload:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.size = len(payload)
        self.offset = 0
        self.read_sizes: list[int] = []

    async def read(self, size: int = -1) -> bytes:
        if size != _UPLOAD_STREAM_CHUNK_BYTES:
            raise AssertionError(f"unbounded multipart read requested: {size}")
        self.read_sizes.append(size)
        chunk = self.payload[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk


class MultipartStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_conversation_upload_stays_path_based(self) -> None:
        line = json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "bounded multipart",
                },
            }
        ).encode("utf-8") + b"\n"
        payload = line * (DATABASE_CONTENT_MAX_BYTES // len(line) + 2)
        upload = _BoundedUpload(payload)
        ingest = AsyncMock(
            return_value=IngestResponse(
                document_id="document-id",
                message="Uploaded successfully",
            )
        )
        metadata = json.dumps(
            {
                "tool": "codex",
                "category": "conversation",
                "content_type": "jsonl",
                "relative_path": "sessions/large.jsonl",
                "hash": "a" * 64,
                "file_size": len(payload),
                "mode": "full",
                "offset": len(payload),
                "metadata": {},
            }
        )

        with (
            patch(
                "server.api.ingest.ensure_device",
                new=AsyncMock(
                    return_value=SimpleNamespace(
                        id=UUID("22222222-2222-2222-2222-222222222222")
                    )
                ),
            ),
            patch(
                "server.api.ingest.store_large_content",
                return_value="raw/private/multipart.txt",
            ),
            patch(
                "server.api.ingest._ingest_or_stage_dependent_delta",
                new=ingest,
            ),
            patch.object(
                Path,
                "read_text",
                side_effect=AssertionError("large multipart read_text"),
            ),
            patch.object(
                Path,
                "read_bytes",
                side_effect=AssertionError("large multipart read_bytes"),
            ),
        ):
            response = await ingest_file_upload(
                metadata=metadata,
                content=upload,  # type: ignore[arg-type]
                _collector_user=SimpleNamespace(
                    id=UUID("11111111-1111-1111-1111-111111111111")
                ),
                _throttle=None,
                db=SimpleNamespace(),  # type: ignore[arg-type]
                x_device_id="device-1",
                x_device_name="test",
                x_device_platform="linux",
            )

        self.assertEqual(response.document_id, "document-id")
        self.assertGreater(len(upload.read_sizes), 1)
        self.assertEqual(set(upload.read_sizes), {_UPLOAD_STREAM_CHUNK_BYTES})
        kwargs = ingest.await_args.kwargs
        ingest_kwargs = kwargs["ingest_kwargs"]
        self.assertEqual(ingest_kwargs["content"], "")
        self.assertFalse(ingest_kwargs["persist_content"])
        self.assertEqual(
            ingest_kwargs["content_s3_key"],
            "raw/private/multipart.txt",
        )
        self.assertIsInstance(
            ingest_kwargs["conversation_source"],
            ConversationFileSource,
        )
        self.assertIsInstance(kwargs["content_path"], Path)
        self.assertNotIn("content_bytes", kwargs)


if __name__ == "__main__":
    unittest.main()
