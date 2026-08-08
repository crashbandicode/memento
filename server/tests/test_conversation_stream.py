from __future__ import annotations

import json
import sys
import tempfile
import tracemalloc
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from server.services.conversation_parser import (  # noqa: E402
    iter_conversation_messages,
    iter_conversation_messages_from_objects,
)
from server.services.conversation_stream import (  # noqa: E402
    ConversationFileSource,
    ConversationSourceChanged,
    iter_decoded_json_objects,
)


class _GuardedReader:
    def __init__(self, stream, *, max_read: int) -> None:
        self._stream = stream
        self._max_read = max_read
        self.read_sizes: list[int] = []

    def __enter__(self):
        self._stream.__enter__()
        return self

    def __exit__(self, *args):
        return self._stream.__exit__(*args)

    def read(self, size: int = -1) -> bytes:
        if size < 1 or size > self._max_read:
            raise AssertionError(f"unbounded source read requested: {size}")
        self.read_sizes.append(size)
        return self._stream.read(size)

    def __getattr__(self, name):
        return getattr(self._stream, name)


class ConversationStreamTests(unittest.TestCase):
    def test_incremental_utf8_decoder_frames_compact_and_pretty_json(self) -> None:
        payload = (
            '{"type":"user","message":{"content":"split 😀"}}\n'
            "{\n"
            '  "type": "assistant",\n'
            '  "message": {"content": "pretty"}\n'
            "}\n"
            "not-json\n"
            '{"type":"user","message":{"content":"after"}}'
        ).encode("utf-8")

        objects = list(iter_decoded_json_objects(bytes((value,)) for value in payload))

        self.assertEqual(
            [obj["message"]["content"] for obj in objects],
            ["split 😀", "pretty", "after"],
        )

    def test_path_source_uses_only_bounded_binary_reads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "large.jsonl"
            records = [
                {
                    "type": "event_msg",
                    "timestamp": f"2026-08-07T12:00:0{index}Z",
                    "payload": {
                        "type": "user_message",
                        "message": f"streamed message {index} 😀",
                    },
                }
                for index in range(8)
            ]
            path.write_bytes(
                b"\n".join(
                    json.dumps(record, ensure_ascii=False).encode("utf-8")
                    for record in records
                )
            )
            original_open = Path.open
            guarded_streams: list[_GuardedReader] = []

            def guarded_open(candidate: Path, *args, **kwargs):
                stream = original_open(candidate, *args, **kwargs)
                if candidate == path and args and args[0] == "rb":
                    guarded = _GuardedReader(stream, max_read=17)
                    guarded_streams.append(guarded)
                    return guarded
                return stream

            with (
                patch.object(Path, "read_text", side_effect=AssertionError("read_text")),
                patch.object(Path, "read_bytes", side_effect=AssertionError("read_bytes")),
                patch.object(Path, "open", new=guarded_open),
            ):
                source = ConversationFileSource.inspect(
                    path,
                    chunk_size=17,
                    prefix_bytes=31,
                )
                parsed = list(
                    iter_conversation_messages_from_objects(
                        source.iter_objects(),
                        "codex",
                    )
                )

            self.assertEqual(
                [message.content for message in parsed],
                [f"streamed message {index} 😀" for index in range(8)],
            )
            self.assertEqual(len(guarded_streams), 2)
            self.assertTrue(
                all(
                    sizes and set(sizes) == {17}
                    for sizes in (stream.read_sizes for stream in guarded_streams)
                )
            )

    def test_verified_source_rejects_bytes_changed_after_proof(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "changed.jsonl"
            path.write_text('{"role":"user","content":"before"}\n', encoding="utf-8")
            source = ConversationFileSource.inspect(path, chunk_size=5)
            with path.open("ab") as stream:
                stream.write(b'{"role":"assistant","content":"after"}\n')

            with self.assertRaises(ConversationSourceChanged):
                list(source.iter_objects())

    def test_normalized_path_parse_memory_does_not_scale_with_file_size(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "many-records.jsonl"
            record_count = 60_000
            record = json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "user_message",
                        "message": "bounded normalized parsing",
                    },
                }
            ).encode("utf-8") + b"\n"
            with path.open("wb") as output:
                for _ in range(record_count):
                    output.write(record)
            file_size = path.stat().st_size

            tracemalloc.start()
            try:
                source = ConversationFileSource.inspect(
                    path,
                    chunk_size=4096,
                    prefix_bytes=1024,
                )
                parsed_count = sum(
                    1
                    for _ in iter_conversation_messages_from_objects(
                        source.iter_objects(),
                        "codex",
                    )
                )
                _current, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()

            self.assertEqual(parsed_count, record_count)
            self.assertGreater(file_size, 5 * 1024 * 1024)
            self.assertLess(peak, file_size // 3)

    def test_streamed_parser_matches_existing_string_parser(self) -> None:
        content = "\n".join(
            (
                json.dumps(
                    {
                        "type": "turn_context",
                        "payload": {
                            "model": "gpt-test",
                            "reasoning_effort": "high",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "event_msg",
                        "timestamp": "2026-08-07T12:00:00Z",
                        "payload": {
                            "type": "user_message",
                            "message": "keep ordering",
                            "turn_id": "turn-1",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "event_msg",
                        "timestamp": "2026-08-07T12:00:01Z",
                        "payload": {
                            "type": "agent_message",
                            "message": "same normalized output",
                        },
                    }
                ),
            )
        )
        expected = list(iter_conversation_messages(content, "codex"))
        streamed = list(
            iter_conversation_messages_from_objects(
                iter_decoded_json_objects(
                    content.encode("utf-8")[index : index + 3]
                    for index in range(0, len(content.encode("utf-8")), 3)
                ),
                "codex",
            )
        )

        self.assertEqual(streamed, expected)


if __name__ == "__main__":
    unittest.main()
