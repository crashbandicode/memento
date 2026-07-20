"""Streaming object storage for raw transcripts too large for PostgreSQL TEXT."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from ..config import settings


def _client():
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        config=Config(signature_version="s3v4"),
        region_name="us-east-1",
    )


def large_content_key(*, user_id: str, device_id: str, job_id: str) -> str:
    """Return the immutable private object key for one durable upload."""
    device_key = hashlib.sha256(device_id.encode("utf-8")).hexdigest()
    return f"raw/{user_id}/{device_key}/{job_id}.txt"


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
