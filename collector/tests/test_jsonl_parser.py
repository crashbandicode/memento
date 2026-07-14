from collector.parsers.jsonl import JsonlParser


def test_unlimited_jsonl_parser_does_not_report_truncation(tmp_path):
    transcript = tmp_path / "conversation.jsonl"
    transcript.write_text(
        '{"type":"user","timestamp":"2026-01-01T00:00:00Z"}\n',
        encoding="utf-8",
    )

    result = JsonlParser().parse(transcript)

    assert result.metadata["total_lines"] == 1
    assert "truncated" not in result.metadata


def test_bounded_jsonl_parser_leaves_an_incomplete_tail_for_the_next_delta(
    tmp_path,
):
    transcript = tmp_path / "conversation.jsonl"
    complete = '{"type":"user","timestamp":"2026-01-01T00:00:00Z"}\n'
    incomplete = '{"type":"assistant","timestamp":"2026-01-01T00:'
    transcript.write_bytes((complete + incomplete).encode("utf-8"))

    result = JsonlParser().parse(
        transcript,
        end_offset=transcript.stat().st_size,
    )

    assert result.content == complete.rstrip("\n")
    assert result.offset == len(complete.encode("utf-8"))
    assert result.metadata["total_lines"] == 1


def test_jsonl_parser_tolerates_non_object_records(tmp_path):
    transcript = tmp_path / "conversation.jsonl"
    transcript.write_text(
        '["transport", "record"]\n'
        '{"type":"user","timestamp":"2026-01-01T00:00:00Z"}\n',
        encoding="utf-8",
    )

    result = JsonlParser().parse(transcript)

    assert result.metadata["total_lines"] == 2
    assert result.metadata["message_types"] == {"user": 1}
