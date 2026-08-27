"""Streaming object storage for raw transcripts too large for PostgreSQL TEXT."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import boto3
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError

from ..config import settings

if TYPE_CHECKING:
    import asyncpg
    from sqlalchemy.ext.asyncio import AsyncSession


DATABASE_CONTENT_MAX_BYTES = 1024 * 1024
DOCUMENT_CONTENT_PREFIX = "document-content/v1"
_STREAM_CHUNK_BYTES = 64 * 1024


class DocumentContentIntegrityError(RuntimeError):
    """An immutable object is absent or does not match its recorded proof."""


class DocumentContentUnavailableError(RuntimeError):
    """A verified document-content object could not be read safely."""


@dataclass(frozen=True, slots=True)
class DocumentContentPointer:
    key: str
    sha256: str
    size_bytes: int
    verified_at: datetime


def _client():
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        config=Config(signature_version="s3v4"),
        region_name="us-east-1",
    )


def document_content_key(*, document_id: object, sha256: str) -> str:
    """Return the deterministic immutable key for one exact document value."""
    if len(sha256) != 64:
        raise ValueError("document content SHA-256 must be 64 hexadecimal characters")
    return f"{DOCUMENT_CONTENT_PREFIX}/{document_id}/{sha256}"


def _is_missing_object_error(exc: BaseException) -> bool:
    if not isinstance(exc, ClientError):
        return False
    code = str(exc.response.get("Error", {}).get("Code", ""))
    return code in {"404", "NoSuchKey", "NoSuchObject", "NotFound"}


def _payload_proof(
    *,
    content: str | None = None,
    payload_path: Path | None = None,
) -> tuple[str, int, bytes | None]:
    """Hash exactly one sanitized text payload without changing its bytes."""
    if (content is None) == (payload_path is None):
        raise ValueError("provide exactly one of content or payload_path")
    if content is not None:
        payload = content.encode("utf-8")
        return hashlib.sha256(payload).hexdigest(), len(payload), payload

    assert payload_path is not None
    digest = hashlib.sha256()
    size = 0
    with payload_path.open("rb", buffering=0) as stream:
        while chunk := stream.read(_STREAM_CHUNK_BYTES):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size, None


def _stream_verified_object(
    key: str,
    *,
    expected_sha256: str,
    expected_size: int,
    s3_client=None,
    collect: bool,
) -> bytes | None:
    """GET an object once and prove its actual streamed bytes before use."""
    client = s3_client or _client()
    response = client.get_object(Bucket=settings.s3_bucket, Key=key)
    body = response["Body"]
    digest = hashlib.sha256()
    size = 0
    chunks: list[bytes] | None = [] if collect else None
    try:
        while chunk := body.read(_STREAM_CHUNK_BYTES):
            digest.update(chunk)
            size += len(chunk)
            if chunks is not None:
                chunks.append(chunk)
    finally:
        close = getattr(body, "close", None)
        if close is not None:
            close()

    actual_sha256 = digest.hexdigest()
    if size != expected_size or actual_sha256 != expected_sha256:
        raise DocumentContentIntegrityError(
            "document content object integrity mismatch "
            f"for {key}: expected {expected_size}/{expected_sha256}, "
            f"got {size}/{actual_sha256}"
        )
    return b"".join(chunks) if chunks is not None else None


def verify_document_content_object(
    key: str,
    *,
    sha256: str,
    size_bytes: int,
    s3_client=None,
) -> None:
    """Stream-GET and verify one immutable document-content object."""
    _stream_verified_object(
        key,
        expected_sha256=sha256,
        expected_size=size_bytes,
        s3_client=s3_client,
        collect=False,
    )


def read_verified_document_content(
    key: str,
    *,
    sha256: str,
    size_bytes: int,
    s3_client=None,
) -> str:
    """Read text only after the immutable object's streamed integrity proof."""
    payload = _stream_verified_object(
        key,
        expected_sha256=sha256,
        expected_size=size_bytes,
        s3_client=s3_client,
        collect=True,
    )
    assert payload is not None
    return payload.decode("utf-8")


def _put_or_reuse_document_content(
    *,
    document_id: object,
    sha256: str,
    size_bytes: int,
    content: str | None = None,
    payload_path: Path | None = None,
    s3_client=None,
) -> DocumentContentPointer:
    """Publish only verified immutable bytes; a conflicting key is fatal."""
    client = s3_client or _client()
    key = document_content_key(document_id=document_id, sha256=sha256)
    try:
        verify_document_content_object(
            key,
            sha256=sha256,
            size_bytes=size_bytes,
            s3_client=client,
        )
    except ClientError as exc:
        if not _is_missing_object_error(exc):
            raise
        extra_args = {
            "Bucket": settings.s3_bucket,
            "Key": key,
            "ContentType": "text/plain; charset=utf-8",
            "Metadata": {
                "memento-document-id": str(document_id),
                "memento-content-sha256": sha256,
                "memento-content-size": str(size_bytes),
            },
        }
        try:
            if content is not None:
                extra_args["Body"] = content.encode("utf-8")
                client.put_object(**extra_args)
            else:
                assert payload_path is not None
                with payload_path.open("rb") as stream:
                    extra_args["Body"] = stream
                    extra_args["ContentLength"] = size_bytes
                    client.put_object(**extra_args)
        except Exception as put_error:
            # An object-store timeout is ambiguous. Probe the deterministic
            # key before reporting failure: a matching completed PUT may be
            # reused, but an absent or mismatched object is never overwritten.
            try:
                verify_document_content_object(
                    key,
                    sha256=sha256,
                    size_bytes=size_bytes,
                    s3_client=client,
                )
            except DocumentContentIntegrityError:
                raise
            except Exception:
                raise put_error
    except DocumentContentIntegrityError:
        # The key is content-addressed. A mismatch is corruption or a hostile
        # overwrite; never replace it with this request's bytes.
        raise

    # A timed-out PUT can still have succeeded. Reusing the deterministic key
    # is safe only after this GET hashes the exact local proof.
    verify_document_content_object(
        key,
        sha256=sha256,
        size_bytes=size_bytes,
        s3_client=client,
    )
    return DocumentContentPointer(
        key=key,
        sha256=sha256,
        size_bytes=size_bytes,
        verified_at=datetime.now(timezone.utc),
    )


async def finalize_document_content(
    *,
    document_id: object,
    content: str | None = None,
    payload_path: Path | None = None,
    db: "AsyncSession | None" = None,
    connection: "asyncpg.Connection | None" = None,
    s3_client=None,
) -> DocumentContentPointer:
    """Run the write-path PUT/reuse/GET verification before pointer commit.

    The caller owns the still-uncommitted SQLAlchemy transaction. It computes
    the sanitized payload proof here, stages normalized rows first, calls this
    finalizer, then assigns the returned pointer and commits everything once.
    """
    sha256, size_bytes, _ = await asyncio.to_thread(
        _payload_proof,
        content=content,
        payload_path=payload_path,
    )
    if db is not None and connection is not None:
        raise ValueError("provide at most one transaction connection")
    if db is not None:
        # GC takes this same transaction-scoped key before its final
        # live-pointer recheck and delete.  Holding it through the caller's
        # pointer commit means a GC winner can only delete before a writer
        # re-verifies/recreates the immutable key, never between verification
        # and publication.
        from sqlalchemy import text

        key = document_content_key(document_id=document_id, sha256=sha256)
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": key},
        )
    elif connection is not None:
        # Phase 2 owns a raw asyncpg transaction.  This is the identical
        # transaction-scoped content-key lock used by the SQLAlchemy caller;
        # it must be held on the connection which later publishes the pointer.
        key = document_content_key(document_id=document_id, sha256=sha256)
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
            key,
        )
    return await asyncio.to_thread(
        _put_or_reuse_document_content,
        document_id=document_id,
        sha256=sha256,
        size_bytes=size_bytes,
        content=content,
        payload_path=payload_path,
        s3_client=s3_client,
    )


def _pointer_is_verified(document: object) -> bool:
    return bool(
        getattr(document, "content_s3_key", None)
        and getattr(document, "content_object_sha256", None)
        and getattr(document, "content_object_size_bytes", None) is not None
        and getattr(document, "content_object_verified_at", None) is not None
    )


async def document_content(
    db: "AsyncSession",
    document: object,
    *,
    s3_client=None,
) -> str | None:
    """Return exact text from a verified immutable pointer, or logical NULL.

    PostgreSQL no longer has an inline fallback.  A pointer read failure is a
    recoverable operational error (restore or reprocess the client source),
    never a reason to synthesize source from normalized messages.
    """
    document_id = getattr(document, "id")
    if _pointer_is_verified(document):
        try:
            return await asyncio.to_thread(
                read_verified_document_content,
                getattr(document, "content_s3_key"),
                sha256=getattr(document, "content_object_sha256"),
                size_bytes=int(getattr(document, "content_object_size_bytes")),
                s3_client=s3_client,
            )
        except (
            BotoCoreError,
            ClientError,
            DocumentContentIntegrityError,
            OSError,
            UnicodeError,
        ) as exc:
            raise DocumentContentUnavailableError(
                f"verified document content for {document_id} is unavailable"
            ) from exc
    return None


async def document_content_prefix(
    db: "AsyncSession",
    document: object,
    *,
    max_chars: int,
    s3_client=None,
) -> str | None:
    """Return the same character-prefix callers previously sliced from TEXT."""
    if max_chars <= 0:
        return ""
    content = await document_content(db, document, s3_client=s3_client)
    return None if content is None else content[:max_chars]


async def document_content_lines(
    db: "AsyncSession",
    document: object,
    *,
    s3_client=None,
) -> list[str]:
    """Expose raw source lines through the single dual-read abstraction."""
    content = await document_content(db, document, s3_client=s3_client)
    return [] if content is None else content.splitlines()


def large_content_key(*, user_id: str, device_id: str, job_id: str) -> str:
    """Return the immutable private object key for one durable upload."""
    device_key = hashlib.sha256(device_id.encode("utf-8")).hexdigest()
    return f"raw/{user_id}/{device_key}/{job_id}.txt"


def multipart_content_job_id(
    *,
    user_id: str,
    device_id: str,
    relative_path: str,
    content_hash: str,
) -> str:
    """Address a retryable multipart source without exposing owned paths."""
    identity = json.dumps(
        [user_id, device_id, relative_path, content_hash],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(b"memento:multipart-content:v1\0" + identity).hexdigest()


def store_large_content(
    payload_path: Path,
    *,
    user_id: str,
    device_id: str,
    job_id: str,
    s3_client=None,
) -> str:
    """Stream one immutable raw payload to MinIO and verify its byte length."""
    client = s3_client or _client()
    bucket = settings.s3_bucket
    key = large_content_key(user_id=user_id, device_id=device_id, job_id=job_id)
    try:
        client.head_bucket(Bucket=bucket)
    except ClientError as exc:
        error_code = str(exc.response.get("Error", {}).get("Code", ""))
        if error_code not in {"404", "NoSuchBucket", "NotFound"}:
            raise
        client.create_bucket(Bucket=bucket)

    client.upload_file(
        str(payload_path),
        bucket,
        key,
        ExtraArgs={"ContentType": "text/plain; charset=utf-8"},
    )
    stored = client.head_object(Bucket=bucket, Key=key)
    expected_size = payload_path.stat().st_size
    if int(stored.get("ContentLength", -1)) != expected_size:
        try:
            client.delete_object(Bucket=bucket, Key=key)
        except Exception:
            pass
        raise OSError("raw transcript object size verification failed")
    return key


def read_large_content_prefix(
    key: str,
    *,
    max_bytes: int = 1024 * 1024,
    s3_client=None,
) -> str:
    """Range-read a bounded UTF-8 prefix from one private transcript."""
    if max_bytes <= 0:
        return ""
    client = s3_client or _client()
    response = client.get_object(
        Bucket=settings.s3_bucket,
        Key=key,
        Range=f"bytes=0-{max_bytes - 1}",
    )
    body = response["Body"]
    try:
        payload = body.read(max_bytes)
    finally:
        close = getattr(body, "close", None)
        if close is not None:
            close()
    return payload.decode("utf-8", errors="replace")


def iter_large_content_lines(
    key: str,
    *,
    chunk_size: int = 64 * 1024,
    s3_client=None,
) -> Iterator[str]:
    """Stream one private UTF-8 transcript without a whole-object size cap.

    Offline repair jobs only need to inspect one JSONL record at a time.  Using
    the bounded whole-object reader for that work made a single large, valid
    transcript abort an otherwise independent corpus repair.  This iterator
    keeps memory proportional to the largest source line and always closes the
    underlying response body when iteration ends.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    client = s3_client or _client()
    response = client.get_object(Bucket=settings.s3_bucket, Key=key)
    body = response["Body"]
    try:
        for line in body.iter_lines(chunk_size=chunk_size, keepends=False):
            yield line.decode("utf-8", errors="replace")
    finally:
        close = getattr(body, "close", None)
        if close is not None:
            close()


def read_large_content(
    key: str,
    *,
    max_bytes: int = 128 * 1024 * 1024,
    s3_client=None,
) -> str:
    """Read one bounded private transcript for an offline repair operation."""
    if max_bytes <= 0:
        return ""
    client = s3_client or _client()
    response = client.get_object(Bucket=settings.s3_bucket, Key=key)
    declared_size = int(response.get("ContentLength", -1))
    if declared_size > max_bytes:
        body = response.get("Body")
        if body is not None and getattr(body, "close", None) is not None:
            body.close()
        raise ValueError(
            f"externalized transcript exceeds repair limit: {declared_size} bytes"
        )
    body = response["Body"]
    try:
        payload = body.read(max_bytes + 1)
    finally:
        close = getattr(body, "close", None)
        if close is not None:
            close()
    if len(payload) > max_bytes:
        raise ValueError("externalized transcript exceeds repair limit")
    return payload.decode("utf-8", errors="replace")


def copy_legacy_large_content_to_path(
    key: str,
    target_path: Path,
    *,
    s3_client=None,
) -> Path:
    """Stream a legacy ``raw/`` object into a local finalizer payload file.

    Legacy keys have no recorded SHA/size proof, so this routine deliberately
    makes no trust claim about them. The document-content finalizer computes a
    new proof and verifies the copied immutable object before switching the DB
    pointer. It avoids the old whole-object repair cap while migrating large
    historical transcripts.
    """
    client = s3_client or _client()
    response = client.get_object(Bucket=settings.s3_bucket, Key=key)
    body = response["Body"]
    try:
        with target_path.open("wb", buffering=0) as target:
            while chunk := body.read(_STREAM_CHUNK_BYTES):
                target.write(chunk)
    finally:
        close = getattr(body, "close", None)
        if close is not None:
            close()
    return target_path
