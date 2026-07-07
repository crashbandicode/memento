"""Durable staging for chunked collector uploads.

The public request path only owns receiving and fsyncing chunks. Expensive
conversation parsing and database ingestion happens after the final request has
received a response, in a dedicated worker. This keeps Cloudflare/client request
timeouts from causing complete multi-hundred-megabyte retransmissions.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

DEFAULT_SPOOL_ROOT = Path(os.environ.get("MEMENTO_INGEST_SPOOL_DIR", "/data/ingest-spool"))
MAX_CHUNKS = 4096
MAX_CHUNK_BYTES = 4 * 1024 * 1024
MAX_UPLOAD_BYTES = 1024 * 1024 * 1024
MAX_SPOOL_BYTES = int(
    os.environ.get("MEMENTO_INGEST_SPOOL_MAX_BYTES", 16 * 1024 * 1024 * 1024)
)
MIN_FREE_BYTES = int(
    os.environ.get("MEMENTO_INGEST_SPOOL_MIN_FREE_BYTES", 2 * 1024 * 1024 * 1024)
)
STALE_INCOMPLETE_SECONDS = 24 * 60 * 60
COMPLETION_RECEIPT_SECONDS = 7 * 24 * 60 * 60
_JOB_ID_RE = re.compile(r"^[0-9a-f]{64}$")


class ChunkValidationError(ValueError):
    """Raised when collector chunk metadata is unsafe or inconsistent."""


def _job_id(meta: dict[str, Any], user_id: str, device_id: str) -> str:
    identity = json.dumps(
        [user_id, device_id, meta.get("upload_id"), meta.get("hash")],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _job_dir(job_id: str, root: Path) -> Path:
    if not _JOB_ID_RE.fullmatch(job_id):
        raise ChunkValidationError("invalid spool job id")
    return root / job_id


@contextmanager
def _filesystem_lock(path: Path, *, blocking: bool = True) -> Iterator[bool]:
    """Cross-process advisory lock for API workers sharing one spool volume."""
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    stream = path.open("a+b")
    os.chmod(path, 0o600)
    flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
    acquired = False
    try:
        try:
            fcntl.flock(stream.fileno(), flags)
            acquired = True
        except BlockingIOError:
            pass
        yield acquired
    finally:
        if acquired:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


@contextmanager
def spool_job_lock(
    job_id: str,
    *,
    root: Path = DEFAULT_SPOOL_ROOT,
    purpose: str = "stage",
    blocking: bool = True,
) -> Iterator[bool]:
    """Lock one validated job; distinct purposes may cover different phases."""
    if not _JOB_ID_RE.fullmatch(job_id):
        raise ChunkValidationError("invalid spool job id")
    with _filesystem_lock(
        root / f".{job_id}.{purpose}.lock", blocking=blocking,
    ) as acquired:
        yield acquired


def _spool_usage_bytes(root: Path) -> int:
    total = 0
    if not root.exists():
        return total
    for job_dir in root.iterdir():
        if not job_dir.is_dir() or not _JOB_ID_RE.fullmatch(job_dir.name):
            continue
        for path in job_dir.iterdir():
            if path.name == "payload.bin" or path.name.startswith("chunk-"):
                try:
                    total += path.stat().st_size
                except OSError:
                    pass
    return total


def _assert_spool_capacity(root: Path, additional_bytes: int) -> None:
    if additional_bytes < 0:
        raise ChunkValidationError("invalid spool capacity request")
    if _spool_usage_bytes(root) + additional_bytes > MAX_SPOOL_BYTES:
        raise ChunkValidationError("ingest spool capacity exceeded")
    try:
        free_bytes = shutil.disk_usage(root).free
    except OSError as exc:
        raise ChunkValidationError("ingest spool free space is unavailable") from exc
    if free_bytes - additional_bytes < MIN_FREE_BYTES:
        raise ChunkValidationError("ingest spool free-space reserve reached")


def _completion_path(job_id: str, root: Path) -> Path:
    if not _JOB_ID_RE.fullmatch(job_id):
        raise ChunkValidationError("invalid spool job id")
    return root / "completed" / f"{job_id}.json"


def _fsync_directory(path: Path) -> None:
    """Persist directory-entry changes on filesystems that support it."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _validated_chunk_coordinates(meta: dict[str, Any], chunk_size: int) -> tuple[int, int]:
    if len(json.dumps(meta, ensure_ascii=False, default=str).encode("utf-8")) > 1024 * 1024:
        raise ChunkValidationError("chunk metadata exceeds 1 MiB")
    if isinstance(meta.get("chunk_index"), bool) or isinstance(meta.get("total_chunks"), bool):
        raise ChunkValidationError("chunk index/count must be integers")
    try:
        chunk_index = int(meta["chunk_index"])
        total_chunks = int(meta["total_chunks"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ChunkValidationError("chunk index/count must be integers") from exc
    if not 1 <= total_chunks <= MAX_CHUNKS:
        raise ChunkValidationError(f"total_chunks must be between 1 and {MAX_CHUNKS}")
    if not 0 <= chunk_index < total_chunks:
        raise ChunkValidationError("chunk_index is outside total_chunks")
    if chunk_size > MAX_CHUNK_BYTES:
        raise ChunkValidationError(f"chunk exceeds {MAX_CHUNK_BYTES} bytes")
    if isinstance(meta.get("file_size"), bool):
        raise ChunkValidationError("file_size must be an integer")
    try:
        file_size = int(meta.get("file_size", 0))
    except (TypeError, ValueError) as exc:
        raise ChunkValidationError("file_size must be an integer") from exc
    if file_size <= 0 or file_size > MAX_UPLOAD_BYTES:
        raise ChunkValidationError(f"file_size must be between 1 and {MAX_UPLOAD_BYTES}")
    if file_size > total_chunks * MAX_CHUNK_BYTES:
        raise ChunkValidationError("total_chunks is inconsistent with file_size")
    string_limits = {
        "upload_id": 8192,
        "hash": 64,
        "tool": 50,
        "relative_path": 8192,
        "category": 50,
        "content_type": 50,
    }
    for field, max_length in string_limits.items():
        value = meta.get(field)
        if not isinstance(value, str) or not value or len(value) > max_length:
            raise ChunkValidationError(f"invalid {field} metadata")
    if meta.get("mode", "full") not in ("full", "delta"):
        raise ChunkValidationError("mode must be full or delta")
    if not isinstance(meta.get("metadata", {}), dict):
        raise ChunkValidationError("metadata must be an object")
    if isinstance(meta.get("offset", 0), bool):
        raise ChunkValidationError("offset must be a non-negative integer")
    try:
        offset = int(meta.get("offset", 0))
    except (TypeError, ValueError) as exc:
        raise ChunkValidationError("offset must be a non-negative integer") from exc
    if offset < 0:
        raise ChunkValidationError("offset must be a non-negative integer")
    timestamp = meta.get("timestamp")
    if timestamp is not None and (
        isinstance(timestamp, bool)
        or not isinstance(timestamp, (int, float))
        or not math.isfinite(timestamp)
    ):
        raise ChunkValidationError("timestamp must be finite and numeric")
    return chunk_index, total_chunks


def stage_chunk(
    *,
    meta: dict[str, Any],
    chunk_data: bytes,
    user_id: str,
    device_id: str,
    device_name: str,
    device_platform: str,
    root: Path = DEFAULT_SPOOL_ROOT,
) -> tuple[str, bool]:
    """Atomically persist one chunk and mark the job ready when complete."""
    chunk_index, total_chunks = _validated_chunk_coordinates(meta, len(chunk_data))
    job_id = _job_id(meta, user_id, device_id)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    with spool_job_lock(job_id, root=root, purpose="stage"):
        if _completion_path(job_id, root).is_file():
            return job_id, True

        job_dir = _job_dir(job_id, root)
        job_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(job_dir, 0o700)

        manifest_path = job_dir / "manifest.json"
        manifest = {
            "job_id": job_id,
            "meta": meta,
            "user_id": user_id,
            "device_id": device_id,
            "device_name": device_name,
            "device_platform": device_platform,
            "total_chunks": total_chunks,
        }
        encoded_manifest = json.dumps(
            manifest, ensure_ascii=False, separators=(",", ":"), default=str,
        ).encode("utf-8")
        if manifest_path.exists():
            try:
                existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ChunkValidationError("existing spool manifest is unreadable") from exc
            immutable_fields = (
                "job_id", "user_id", "device_id", "device_name",
                "device_platform", "total_chunks",
            )
            if any(existing.get(field) != manifest.get(field) for field in immutable_fields):
                raise ChunkValidationError("chunk metadata conflicts with existing upload")
            existing_meta = existing.get("meta", {})
            for field in (
                "upload_id", "hash", "tool", "relative_path", "category",
                "content_type", "mode", "offset", "file_size", "sync_strategy",
                "metadata", "timestamp", "total_chunks",
            ):
                if existing_meta.get(field) != meta.get(field):
                    raise ChunkValidationError("chunk metadata conflicts with existing upload")
        else:
            _atomic_write(manifest_path, encoded_manifest)

        chunk_path = job_dir / f"chunk-{chunk_index:06d}.bin"
        if chunk_path.exists():
            if chunk_path.read_bytes() != chunk_data:
                raise ChunkValidationError("duplicate chunk content does not match")
        else:
            received_bytes = sum(
                path.stat().st_size for path in job_dir.glob("chunk-*.bin")
            )
            if received_bytes + len(chunk_data) > int(meta["file_size"]):
                raise ChunkValidationError("received chunks exceed declared file_size")
            with _filesystem_lock(root / ".quota.lock"):
                _assert_spool_capacity(root, len(chunk_data))
                _atomic_write(chunk_path, chunk_data)

        complete = all(
            (job_dir / f"chunk-{index:06d}.bin").is_file()
            for index in range(total_chunks)
        )
        if complete:
            assembled_size = sum(
                (job_dir / f"chunk-{index:06d}.bin").stat().st_size
                for index in range(total_chunks)
            )
            if assembled_size != int(meta["file_size"]):
                raise ChunkValidationError(
                    "received chunks do not match declared file_size"
                )
            if not (job_dir / "ready").exists():
                _atomic_write(job_dir / "ready", b"ready\n")
        return job_id, complete


def ready_job_ids(root: Path = DEFAULT_SPOOL_ROOT) -> list[str]:
    """Return safe, durably-ready job identifiers for recovery."""
    if not root.exists():
        return []
    return sorted(
        path.name
        for path in root.iterdir()
        if path.is_dir()
        and _JOB_ID_RE.fullmatch(path.name)
        and (path / "ready").is_file()
        and (path / "manifest.json").is_file()
        and not (path / "failed.json").exists()
    )


def failed_job_ids(root: Path = DEFAULT_SPOOL_ROOT) -> list[str]:
    """Return quarantined jobs for operational visibility/manual recovery."""
    if not root.exists():
        return []
    return sorted(
        path.name
        for path in root.iterdir()
        if path.is_dir()
        and _JOB_ID_RE.fullmatch(path.name)
        and (path / "failed.json").is_file()
    )


def assemble_job(
    job_id: str, root: Path = DEFAULT_SPOOL_ROOT,
) -> tuple[dict[str, Any], Path]:
    """Assemble a ready job to disk without retaining all chunks in memory."""
    job_dir = _job_dir(job_id, root)
    if not (job_dir / "ready").is_file():
        raise FileNotFoundError(f"spool job {job_id} is not ready")
    manifest = json.loads((job_dir / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("job_id") != job_id:
        raise ChunkValidationError("spool manifest job id does not match")
    total_chunks = int(manifest["total_chunks"])
    if not 1 <= total_chunks <= MAX_CHUNKS:
        raise ChunkValidationError("spool manifest chunk count is invalid")
    expected_size = int(manifest.get("meta", {}).get("file_size", 0))
    if not 1 <= expected_size <= MAX_UPLOAD_BYTES:
        raise ChunkValidationError("spool manifest file_size is invalid")
    payload_path = job_dir / "payload.bin"
    if not payload_path.exists():
        temporary = job_dir / f".payload.{uuid.uuid4().hex}.tmp"
        try:
            with _filesystem_lock(root / ".quota.lock"):
                _assert_spool_capacity(root, expected_size)
                with temporary.open("wb") as output:
                    for index in range(total_chunks):
                        chunk_path = job_dir / f"chunk-{index:06d}.bin"
                        if not chunk_path.is_file():
                            raise FileNotFoundError(
                                f"spool job {job_id} lost chunk {index}"
                            )
                        with chunk_path.open("rb") as source:
                            shutil.copyfileobj(source, output, length=1024 * 1024)
                    output.flush()
                    os.fsync(output.fileno())
                if temporary.stat().st_size != expected_size:
                    raise ChunkValidationError(
                        "assembled payload size does not match file_size"
                    )
                os.chmod(temporary, 0o600)
                os.replace(temporary, payload_path)
                _fsync_directory(job_dir)
        finally:
            temporary.unlink(missing_ok=True)
    if payload_path.stat().st_size != expected_size:
        raise ChunkValidationError("assembled payload size does not match file_size")
    return manifest, payload_path


def remove_job(job_id: str, root: Path = DEFAULT_SPOOL_ROOT) -> None:
    """Delete one validated spool job after its DB transaction commits."""
    job_dir = _job_dir(job_id, root)
    if job_dir.parent.resolve() != root.resolve():
        raise ChunkValidationError("unsafe spool cleanup path")
    with spool_job_lock(job_id, root=root, purpose="stage"):
        shutil.rmtree(job_dir, ignore_errors=True)
        _fsync_directory(root)


def mark_job_complete(
    job_id: str,
    *,
    document_id: str,
    root: Path = DEFAULT_SPOOL_ROOT,
) -> None:
    """Durably retain a short-lived receipt before deleting accepted chunks."""
    completed_dir = root / "completed"
    completed_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(completed_dir, 0o700)
    payload = json.dumps(
        {
            "job_id": job_id,
            "document_id": document_id,
            "completed_at": time.time(),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    _atomic_write(_completion_path(job_id, root), payload)


def mark_job_failed(
    job_id: str,
    *,
    error_type: str,
    attempts: int,
    root: Path = DEFAULT_SPOOL_ROOT,
) -> None:
    """Quarantine a deterministic/terminal job without deleting its evidence."""
    safe_error_type = re.sub(r"[^A-Za-z0-9_.-]", "_", error_type)[:128]
    payload = json.dumps(
        {
            "job_id": job_id,
            "error_type": safe_error_type,
            "attempts": max(0, int(attempts)),
            "failed_at": time.time(),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    with spool_job_lock(job_id, root=root, purpose="stage"):
        job_dir = _job_dir(job_id, root)
        if not job_dir.is_dir():
            raise FileNotFoundError(f"spool job {job_id} is missing")
        _atomic_write(job_dir / "failed.json", payload)


def record_job_attempt(
    job_id: str,
    *,
    root: Path = DEFAULT_SPOOL_ROOT,
) -> int:
    """Persist an attempt before work so hard worker loss is still counted."""
    with spool_job_lock(job_id, root=root, purpose="stage"):
        job_dir = _job_dir(job_id, root)
        if not job_dir.is_dir():
            raise FileNotFoundError(f"spool job {job_id} is missing")
        attempt_path = job_dir / "attempts.json"
        previous = 0
        if attempt_path.exists():
            try:
                previous = max(
                    0,
                    int(json.loads(attempt_path.read_text(encoding="utf-8"))["attempts"]),
                )
            except (KeyError, OSError, TypeError, ValueError):
                previous = 0
        attempts = previous + 1
        payload = json.dumps(
            {"attempts": attempts, "last_attempt_at": time.time()},
            separators=(",", ":"),
        ).encode("utf-8")
        _atomic_write(attempt_path, payload)
        return attempts


def cleanup_completion_receipts(
    *,
    root: Path = DEFAULT_SPOOL_ROOT,
    max_age_seconds: int = COMPLETION_RECEIPT_SECONDS,
) -> int:
    """Expire old idempotency receipts after the collector retry window."""
    completed_dir = root / "completed"
    if not completed_dir.exists():
        return 0
    now = time.time()
    removed = 0
    for receipt in completed_dir.glob("*.json"):
        if not _JOB_ID_RE.fullmatch(receipt.stem):
            continue
        try:
            if now - receipt.stat().st_mtime <= max_age_seconds:
                continue
            receipt.unlink()
            removed += 1
        except OSError:
            continue
    if removed:
        _fsync_directory(completed_dir)
    return removed


def cleanup_stale_incomplete_jobs(
    *,
    root: Path = DEFAULT_SPOOL_ROOT,
    max_age_seconds: int = STALE_INCOMPLETE_SECONDS,
) -> int:
    """Remove abandoned, incomplete uploads while preserving ready work."""
    if not root.exists():
        return 0
    now = time.time()
    removed = 0
    for job_dir in root.iterdir():
        if not job_dir.is_dir() or not _JOB_ID_RE.fullmatch(job_dir.name):
            continue
        with spool_job_lock(job_dir.name, root=root, purpose="stage"):
            # Recheck every predicate while holding the same lock used to
            # persist a final chunk/ready marker. Otherwise cleanup can decide
            # a job is stale, wait for the writer, then delete newly-ready work.
            if not job_dir.is_dir() or (job_dir / "ready").exists():
                continue
            try:
                newest_mtime = max(
                    (entry.stat().st_mtime for entry in job_dir.iterdir()),
                    default=job_dir.stat().st_mtime,
                )
            except OSError:
                continue
            if now - newest_mtime <= max_age_seconds:
                continue
            shutil.rmtree(job_dir, ignore_errors=True)
            _fsync_directory(root)
            removed += 1
    return removed
