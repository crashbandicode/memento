from datetime import datetime, timedelta, timezone
import unittest

from server.services.history_recovery import (
    UserOccurrence,
    partition_recovered_occurrences,
    recovered_occurrence_anchors,
)


BASE = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


def occurrence(
    key: str,
    content: str,
    seconds: int | None,
    line: int = 0,
) -> UserOccurrence:
    return UserOccurrence(
        key=key,
        content=content,
        timestamp=BASE + timedelta(seconds=seconds) if seconds is not None else None,
        line_number=line,
    )


class HistoryRecoveryTests(unittest.TestCase):
    def test_delayed_rollout_copy_matches_history_submission(self) -> None:
        matched, missing = partition_recovered_occurrences(
            [occurrence("source", "deploy it", 128, 40)],
            [occurrence("history", "deploy it", 0)],
        )

        self.assertEqual([row.key for row in matched], ["history"])
        self.assertEqual(missing, [])

    def test_matching_is_one_to_one_for_repeated_prompts(self) -> None:
        history = [
            occurrence("history-1", "keep going", 0),
            occurrence("history-2", "keep going", 5),
        ]

        matched, missing = partition_recovered_occurrences(
            [occurrence("source-1", "keep going", 6, 10)],
            history,
        )

        self.assertEqual([row.key for row in matched], ["history-2"])
        self.assertEqual([row.key for row in missing], ["history-1"])

    def test_same_text_outside_transport_window_is_not_collapsed(self) -> None:
        matched, missing = partition_recovered_occurrences(
            [occurrence("source", "status?", 601, 20)],
            [occurrence("history", "status?", 0)],
        )

        self.assertEqual(matched, [])
        self.assertEqual([row.key for row in missing], ["history"])

    def test_nearest_pairing_does_not_reduce_match_cardinality(self) -> None:
        matched, missing = partition_recovered_occurrences(
            [
                occurrence("source-1", "repeat", 0, 10),
                occurrence("source-2", "repeat", 290, 20),
            ],
            [
                occurrence("history-1", "repeat", 150),
                occurrence("history-2", "repeat", 430),
            ],
        )

        self.assertEqual(
            [row.key for row in matched],
            ["history-1", "history-2"],
        )
        self.assertEqual(missing, [])

    def test_missing_prompt_is_anchored_before_next_source_event(self) -> None:
        anchors = recovered_occurrence_anchors(
            [
                occurrence("line-10", "assistant", 0, 10),
                occurrence("line-20", "tool", 120, 20),
                occurrence("line-30", "assistant", 240, 30),
            ],
            [
                occurrence("between", "missing", 60),
                occurrence("after", "missing later", 300),
                occurrence("untimed", "missing no timestamp", None),
            ],
        )

        self.assertEqual(
            anchors,
            {"between": 20, "after": 31, "untimed": 31},
        )
