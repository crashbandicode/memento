from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from server.services.conversation_stream import ConversationFileSource  # noqa: E402
from server.services.ingest_spool import (  # noqa: E402
    assemble_job_chain,
    ready_manifest,
    stage_chunk,
)
from server.tasks.ingest_spool import _ingest_ready_job  # noqa: E402


class _Session:
    def __init__(self) -> None:
        self.execute = AsyncMock()
        self.commit = AsyncMock()


class _SessionContext:
    def __init__(self, session: _Session) -> None:
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *_args):
        return False


class SpoolStreamingTests(unittest.IsolatedAsyncioTestCase):
    def test_bounded_delta_chain_is_assembled_without_read_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "spool"
            jobs = []
            previous_hash = "base"
            previous_offset = 0
            expected = bytearray()
            for index, payload in enumerate((b'{"a":1}\n', b'{"b":2}\n')):
                content_hash = f"hash-{index}"
                meta = {
                    "upload_id": f"delta-{index}",
                    "hash": content_hash,
                    "tool": "codex",
                    "relative_path": "sessions/thread.jsonl",
                    "category": "conversation",
                    "content_type": "jsonl",
                    "mode": "delta",
                    "chunk_index": 0,
                    "total_chunks": 1,
                    "file_size": len(payload),
                    "offset": previous_offset + len(payload),
                    "base_hash": previous_hash,
                    "base_offset": previous_offset,
                    "metadata": {},
                }
                staged = stage_chunk(
                    meta=meta,
                    chunk_data=payload,
                    user_id="11111111-1111-1111-1111-111111111111",
                    device_id="device-1",
                    device_name="test",
                    device_platform="linux",
                    root=root,
                )
                jobs.append((staged.job_id, ready_manifest(staged.job_id, root)))
                previous_hash = content_hash
                previous_offset += len(payload)
                expected.extend(payload)

            with patch.object(
                Path,
                "read_bytes",
                side_effect=AssertionError("delta chain read_bytes"),
            ):
                chain_path = assemble_job_chain(jobs, root)

            self.assertEqual(chain_path.read_bytes(), bytes(expected))

    async def test_externalized_full_never_materializes_sanitized_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            payload_path = Path(temporary) / "payload.bin"
            payload = "\n".join(
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": f"message {index}",
                        },
                    }
                )
                for index in range(32)
            ).encode("utf-8")
            payload_path.write_bytes(payload)
            job_id = "a" * 64
            manifest = {
                "job_id": job_id,
                "user_id": "11111111-1111-1111-1111-111111111111",
                "device_id": "device-1",
                "device_name": "test",
                "device_platform": "linux",
                "total_chunks": 1,
                "meta": {
                    "tool": "codex",
                    "category": "conversation",
                    "content_type": "jsonl",
                    "relative_path": "sessions/thread.jsonl",
                    "hash": "b" * 64,
                    "file_size": len(payload),
                    "mode": "full",
                    "offset": len(payload),
                    "metadata": {},
                    "authoritative_rebase": True,
                },
            }
            device_session = _Session()
            ingest_session = _Session()
            sessions = iter((device_session, ingest_session))

            def session_factory():
                return _SessionContext(next(sessions))

            document = SimpleNamespace(
                id=UUID("22222222-2222-2222-2222-222222222222"),
                file_size_bytes=len(payload),
                tool_id="codex",
                content_hash="b" * 64,
                _memento_ingest_disposition="idempotent",
            )
            ingest = AsyncMock(return_value=document)

            with (
                patch(
                    "server.tasks.ingest_spool.async_session_factory",
                    side_effect=session_factory,
                ),
                patch(
                    "server.tasks.ingest_spool.ensure_device",
                    new=AsyncMock(
                        return_value=SimpleNamespace(
                            id=UUID("33333333-3333-3333-3333-333333333333")
                        )
                    ),
                ),
                patch(
                    "server.tasks.ingest_spool.assemble_job",
                    return_value=(manifest, payload_path),
                ),
                patch(
                    "server.tasks.ingest_spool.store_large_content",
                    return_value="raw/private/job.txt",
                ),
                patch(
                    "server.tasks.ingest_spool.ingest_file",
                    new=ingest,
                ),
                patch(
                    "server.tasks.ingest_spool.DATABASE_CONTENT_MAX_BYTES",
                    1,
                ),
                patch.object(
                    Path,
                    "read_text",
                    side_effect=AssertionError("large transcript read_text"),
                ),
                patch.object(
                    Path,
                    "read_bytes",
                    side_effect=AssertionError("large transcript read_bytes"),
                ),
            ):
                result = await _ingest_ready_job(job_id, manifest)

            self.assertEqual(result["document_id"], str(document.id))
            kwargs = ingest.await_args.kwargs
            self.assertEqual(kwargs["content"], "")
            self.assertFalse(kwargs["persist_content"])
            self.assertEqual(kwargs["content_s3_key"], "raw/private/job.txt")
            self.assertIsInstance(
                kwargs["conversation_source"],
                ConversationFileSource,
            )
            self.assertEqual(kwargs["conversation_source"].size, len(payload))


if __name__ == "__main__":
    unittest.main()
