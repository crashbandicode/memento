"""Verified document-content reads for direct MCP database mode.

This deliberately mirrors the server's rollout reader without importing the
server package: the MCP sidecar is deployed independently and may run on the
collector's machine.  Object storage is opt-in here.  With no complete S3
configuration, direct mode retains its historical PostgreSQL-only behavior.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import dataclass

import boto3
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError


_STREAM_CHUNK_BYTES = 64 * 1024


class DocumentContentIntegrityError(RuntimeError):
    """The immutable object did not match its database proof."""


class DocumentContentUnavailableError(RuntimeError):
    """Neither verified object storage nor PostgreSQL has readable content."""


@dataclass(frozen=True, slots=True)
class DocumentContentS3Config:
    endpoint: str
    access_key: str
    secret_key: str
    bucket: str


def document_content_s3_config() -> DocumentContentS3Config | None:
    """Return direct-mode S3 configuration only when it is explicitly set.

    The server's development defaults must never make a client-side sidecar
    unexpectedly dial ``localhost:9000``.  All four variables are therefore
    required to enable object reads on a client machine.
    """
    values = {
        "endpoint": os.environ.get("MEMENTO_S3_ENDPOINT", "").strip(),
        "access_key": os.environ.get("MEMENTO_S3_ACCESS_KEY", "").strip(),
        "secret_key": os.environ.get("MEMENTO_S3_SECRET_KEY", "").strip(),
        "bucket": os.environ.get("MEMENTO_S3_BUCKET", "").strip(),
    }
    if not all(values.values()):
        return None
    return DocumentContentS3Config(**values)


def _client(config: DocumentContentS3Config):
    return boto3.client(
        "s3",
        endpoint_url=config.endpoint,
        aws_access_key_id=config.access_key,
        aws_secret_access_key=config.secret_key,
        config=Config(signature_version="s3v4"),
        region_name="us-east-1",
    )


def _pointer_is_verified(document: object) -> bool:
    return bool(
        getattr(document, "content_s3_key", None)
        and getattr(document, "content_object_sha256", None)
        and getattr(document, "content_object_size_bytes", None) is not None
        and getattr(document, "content_object_verified_at", None) is not None
    )


def read_verified_document_content(
    key: str,
    *,
    sha256: str,
    size_bytes: int,
    config: DocumentContentS3Config,
    s3_client=None,
) -> str:
    """Stream one object and return it only after its stored proof matches."""
    client = s3_client or _client(config)
    response = client.get_object(Bucket=config.bucket, Key=key)
    body = response["Body"]
    digest = hashlib.sha256()
    size = 0
    chunks: list[bytes] = []
    try:
        while chunk := body.read(_STREAM_CHUNK_BYTES):
            digest.update(chunk)
            size += len(chunk)
            chunks.append(chunk)
    finally:
        close = getattr(body, "close", None)
        if close is not None:
            close()

    actual_sha256 = digest.hexdigest()
    if size != size_bytes or actual_sha256 != sha256:
        raise DocumentContentIntegrityError(
            "document content object integrity mismatch "
            f"for {key}: expected {size_bytes}/{sha256}, "
            f"got {size}/{actual_sha256}"
        )
    return b"".join(chunks).decode("utf-8")


async def _inline_document_content(db, document_id: object) -> str | None:
    """Hydrate the deferred compatibility column only after object fallback."""
    from sqlalchemy import select

    from .db import Document

    return (
        await db.execute(select(Document.content).where(Document.id == document_id))
    ).scalar_one_or_none()


async def document_content(
    db,
    document: object,
    *,
    s3_config: DocumentContentS3Config | None = None,
    s3_client=None,
) -> str | None:
    """Prefer a verified object, preserving PostgreSQL as the exact fallback."""
    document_id = getattr(document, "id")
    config = s3_config if s3_config is not None else document_content_s3_config()
    if config is not None and _pointer_is_verified(document):
        try:
            return await asyncio.to_thread(
                read_verified_document_content,
                getattr(document, "content_s3_key"),
                sha256=getattr(document, "content_object_sha256"),
                size_bytes=int(getattr(document, "content_object_size_bytes")),
                config=config,
                s3_client=s3_client,
            )
        except (
            BotoCoreError,
            ClientError,
            DocumentContentIntegrityError,
            OSError,
            UnicodeError,
        ):
            # During dual-read the inline value remains authoritative if an
            # optional client-side object-store path is unavailable or corrupt.
            pass

    inline = await _inline_document_content(db, document_id)
    if inline is not None:
        return inline
    if config is not None and _pointer_is_verified(document):
        raise DocumentContentUnavailableError(
            f"verified document content for {document_id} is unavailable"
        )
    return None
