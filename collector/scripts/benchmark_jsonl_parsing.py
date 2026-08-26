"""Compare stdlib and orjson parsing on fat transcript-shaped JSONL records.

Run from ``collector/`` with ``python scripts/benchmark_jsonl_parsing.py``.
The input deliberately resembles a large assistant-tool transcript row rather
than a tiny synthetic JSON object, so it exercises the collector's hot path.
"""

from __future__ import annotations

import json
from statistics import median
from time import perf_counter

import orjson


RECORD_COUNT = 1_000
REPEATS = 7
_TOOL_OUTPUT = (
    "src/collector/parsers/jsonl.py\n"
    "@@ -120,7 +120,7 @@\n"
    "+ parsed transcript payload with nested tool output\n"
) * 48


def _records() -> tuple[str, ...]:
    return tuple(
        json.dumps(
            {
                "type": "assistant",
                "uuid": f"message-{index:04d}",
                "timestamp": f"2026-08-26T12:{index % 60:02d}:00.000Z",
                "sessionId": "4fd5e988-e1aa-46ea-a633-d8d5d38566a8",
                "message": {
                    "role": "assistant",
                    "model": "claude-opus-4-1",
                    "content": [
                        {"type": "text", "text": "Investigating the parsing hot path."},
                        {
                            "type": "tool_use",
                            "id": f"toolu_{index:04d}",
                            "name": "Read",
                            "input": {
                                "file_path": "collector/collector/parsers/jsonl.py",
                                "offset": index,
                                "limit": 400,
                                "captured_output": _TOOL_OUTPUT,
                            },
                        },
                    ],
                    "usage": {"input_tokens": 12_345, "output_tokens": 6_789},
                },
                "metadata": {
                    "cwd": "C:/Users/example/source/memento-control-plane",
                    "git_branch": "performance/orjson-jsonl",
                    "tags": ["collector", "transcript", "synchronization"],
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        for index in range(RECORD_COUNT)
    )


def _seconds(loads, records: tuple[str, ...]) -> float:  # type: ignore[no-untyped-def]
    samples: list[float] = []
    for _ in range(REPEATS):
        started = perf_counter()
        for record in records:
            loads(record)
        samples.append(perf_counter() - started)
    return median(samples)


def main() -> None:
    records = _records()
    payload_bytes = sum(len(record.encode("utf-8")) + 1 for record in records)
    stdlib_seconds = _seconds(json.loads, records)
    orjson_seconds = _seconds(orjson.loads, records)
    speedup = stdlib_seconds / orjson_seconds

    print(f"records: {len(records):,}")
    print(f"payload: {payload_bytes / (1024 * 1024):.2f} MiB")
    print(f"stdlib json.loads median: {stdlib_seconds * 1_000:.2f} ms")
    print(f"orjson.loads median: {orjson_seconds * 1_000:.2f} ms")
    print(f"speedup: {speedup:.2f}x")


if __name__ == "__main__":
    main()
