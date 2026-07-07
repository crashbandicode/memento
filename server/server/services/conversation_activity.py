"""Shared conversation activity classification for list surfaces."""

from __future__ import annotations

from collections.abc import Iterable, Mapping


SHORT_EXCHANGE_CHARACTER_LIMIT = 120


def is_low_activity_summary(
    user_count: int,
    assistant_count: int,
    human_character_count: int,
) -> bool:
    """Return whether a thread is empty or too slight for the primary list.

    A useful exchange needs input from both sides. A single Q/A is retained
    when it contains enough substance; tiny acknowledgements are tucked into
    the collapsed low-activity section instead.
    """
    if user_count <= 0 or assistant_count <= 0:
        return True
    return (
        user_count + assistant_count <= 2
        and human_character_count < SHORT_EXCHANGE_CHARACTER_LIMIT
    )


def is_low_activity_messages(messages: Iterable[Mapping[str, object]]) -> bool:
    """Classify normalized message dictionaries using the shared heuristic."""
    user_count = 0
    assistant_count = 0
    character_count = 0
    for message in messages:
        role = message.get("role")
        if role == "user":
            user_count += 1
        elif role == "assistant":
            assistant_count += 1
        else:
            continue
        character_count += len(str(message.get("content") or "").strip())

    return is_low_activity_summary(
        user_count,
        assistant_count,
        character_count,
    )
