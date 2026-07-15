"""Reconcile Codex history prompts with normalized rollout messages."""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime
from typing import Hashable, Iterable


HISTORY_MATCH_TOLERANCE_SECONDS = 5 * 60


@dataclass(frozen=True)
class UserOccurrence:
    """One user prompt occurrence from a rollout or Codex history.jsonl."""

    key: Hashable
    content: str
    timestamp: datetime | None
    line_number: int = 0


def partition_recovered_occurrences(
    source_rows: Iterable[UserOccurrence],
    recovered_rows: Iterable[UserOccurrence],
    *,
    tolerance_seconds: int = HISTORY_MATCH_TOLERANCE_SECONDS,
) -> tuple[list[UserOccurrence], list[UserOccurrence]]:
    """Return recovered occurrences already represented by source vs missing.

    Matching is one-to-one within identical normalized content. Codex records
    history at submission time and writes the rollout event later, so exact
    timestamp equality is not a valid identity. A bounded nearest-time match
    removes transport duplicates without collapsing legitimately repeated
    prompts: every source occurrence can satisfy at most one history row.
    """
    source_by_content: dict[str, list[UserOccurrence]] = {}
    for row in source_rows:
        source_by_content.setdefault(row.content, []).append(row)

    recovered = list(recovered_rows)
    recovered_by_content: dict[str, list[tuple[int, UserOccurrence]]] = {}
    for index, row in enumerate(recovered):
        recovered_by_content.setdefault(row.content, []).append((index, row))

    matched_history_indexes: set[int] = set()
    for content, indexed_history in recovered_by_content.items():
        source = source_by_content.get(content, [])
        if not source:
            continue

        # Ordered dynamic programming gives a maximum-cardinality one-to-one
        # match. Among equally complete matches, it chooses the smallest total
        # capture delay. Keeping only two score rows bounds memory; one byte per
        # cell retains the path needed to identify the matched history rows.
        history_count = len(indexed_history)
        source_count = len(source)
        width = source_count + 1
        directions = bytearray((history_count + 1) * width)
        previous_matches = [0] * width
        previous_cost = [0.0] * width
        for history_position, (_, history_row) in enumerate(
            indexed_history,
            start=1,
        ):
            current_matches = [0] * width
            current_cost = [0.0] * width
            for source_position, source_row in enumerate(source, start=1):
                # 1 = skip history, 2 = skip source, 3 = match.
                best_matches = previous_matches[source_position]
                best_cost = previous_cost[source_position]
                direction = 1
                left_matches = current_matches[source_position - 1]
                left_cost = current_cost[source_position - 1]
                if (left_matches, -left_cost) > (best_matches, -best_cost):
                    best_matches = left_matches
                    best_cost = left_cost
                    direction = 2

                if history_row.timestamp is None or source_row.timestamp is None:
                    distance = 0.0
                else:
                    distance = abs(
                        (
                            source_row.timestamp - history_row.timestamp
                        ).total_seconds()
                    )
                if distance <= tolerance_seconds:
                    diagonal_matches = (
                        previous_matches[source_position - 1] + 1
                    )
                    diagonal_cost = (
                        previous_cost[source_position - 1] + distance
                    )
                    if (diagonal_matches, -diagonal_cost) > (
                        best_matches,
                        -best_cost,
                    ):
                        best_matches = diagonal_matches
                        best_cost = diagonal_cost
                        direction = 3

                current_matches[source_position] = best_matches
                current_cost[source_position] = best_cost
                directions[history_position * width + source_position] = direction
            previous_matches = current_matches
            previous_cost = current_cost

        history_position = history_count
        source_position = source_count
        while history_position and source_position:
            direction = directions[history_position * width + source_position]
            if direction == 3:
                recovered_index, _ = indexed_history[history_position - 1]
                matched_history_indexes.add(recovered_index)
                history_position -= 1
                source_position -= 1
            elif direction == 2:
                source_position -= 1
            else:
                history_position -= 1

    matched = [
        row for index, row in enumerate(recovered)
        if index in matched_history_indexes
    ]
    missing = [
        row for index, row in enumerate(recovered)
        if index not in matched_history_indexes
    ]
    return matched, missing


def recovered_occurrence_anchors(
    source_timeline: Iterable[UserOccurrence],
    recovered_rows: Iterable[UserOccurrence],
) -> dict[Hashable, int]:
    """Map missing prompts to the source line they should appear before."""
    source = list(source_timeline)
    max_line = max((row.line_number for row in source), default=0)
    timestamped = sorted(
        (
            (row.timestamp.timestamp(), row.line_number)
            for row in source
            if row.timestamp is not None
        ),
        key=lambda value: (value[0], value[1]),
    )
    timestamp_values = [value[0] for value in timestamped]

    anchors: dict[Hashable, int] = {}
    for recovered in recovered_rows:
        if recovered.timestamp is None or not timestamped:
            anchors[recovered.key] = max_line + 1
            continue
        index = bisect_left(timestamp_values, recovered.timestamp.timestamp())
        anchors[recovered.key] = (
            timestamped[index][1] if index < len(timestamped) else max_line + 1
        )
    return anchors
