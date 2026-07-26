from __future__ import annotations

from server.tasks import db_backup


def test_backup_covers_both_embedding_tables() -> None:
    quality = db_backup.TABLES.index("document_embeddings")
    fast = db_backup.TABLES.index("document_embeddings_fast")
    messages = db_backup.TABLES.index("conversation_messages")

    assert quality < fast < messages


def test_large_backup_buffer_spills_out_of_memory() -> None:
    with db_backup._backup_buffer(max_memory_bytes=8) as buffer:
        buffer.write(b"larger than eight bytes")
        assert getattr(buffer, "_rolled", False) is True
