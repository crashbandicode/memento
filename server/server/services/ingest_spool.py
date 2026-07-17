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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from uuid import UUID

from .ingest_revision import (
    bounded_source_timestamp,
    full_snapshot_revision,
    normalized_source_timestamp,
)

DEFAULT_SPOOL_ROOT = Path(
    os.environ.get("MEMENTO_INGEST_SPOOL_DIR", "/data/ingest-spool")
)
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
_MAX_MANIFEST_BYTES = 1024 * 1024
SourceIdentity = tuple[str, str, str, str]


class ChunkValidationError(ValueError):
    """Raised when collector chunk metadata is unsafe or inconsistent."""


@dataclass(frozen=True)
class StagedChunk:
    """Result of durably staging one chunk.

    Iteration intentionally preserves the historical ``(job_id, complete)``
    contract for callers that unpack the result. ``should_enqueue`` separates
    a newly-ready job from an idempotent retry backed by a completion receipt.
    """

    job_id: str
    complete: bool
    should_enqueue: bool

    def __iter__(self):
        yield self.job_id
        yield self.complete


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
        root / f".{job_id}.{purpose}.lock",
        blocking=blocking,
    ) as acquired:
        yield acquired


@contextmanager
def spool_source_lock(
    identity: SourceIdentity,
    *,
    root: Path = DEFAULT_SPOOL_ROOT,
    blocking: bool = True,
) -> Iterator[bool]:
    """Serialize finalization for one owned source without exposing its path."""
    if len(identity) != 4 or any(not value for value in identity):
        raise ChunkValidationError("invalid spool source identity")
    encoded = json.dumps(
        identity,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(b"memento:spool-source:v1\0" + encoded).hexdigest()
    with _filesystem_lock(
        root / ".source-locks" / f"{digest}.lock",
        blocking=blocking,
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
            if path.name in {"payload.bin", "sanitized.bin"} or path.name.startswith(
                "chunk-"
            ):
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


def has_completion_receipt(
    *,
    meta: dict[str, Any],
    user_id: str,
    device_id: str,
    root: Path = DEFAULT_SPOOL_ROOT,
) -> bool:
    """Return whether this exact authenticated upload was already committed."""
    return _completion_path(_job_id(meta, user_id, device_id), root).is_file()


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


def _validated_chunk_coordinates(
    meta: dict[str, Any], chunk_size: int
) -> tuple[int, int]:
    if (
        len(json.dumps(meta, ensure_ascii=False, default=str).encode("utf-8"))
        > 1024 * 1024
    ):
        raise ChunkValidationError("chunk metadata exceeds 1 MiB")
    if isinstance(meta.get("chunk_index"), bool) or isinstance(
        meta.get("total_chunks"), bool
    ):
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
        raise ChunkValidationError(
            f"file_size must be between 1 and {MAX_UPLOAD_BYTES}"
        )
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
    base_hash = meta.get("base_hash")
    base_offset = meta.get("base_offset")
    if base_hash is not None and (
        not isinstance(base_hash, str) or not base_hash or len(base_hash) > 64
    ):
        raise ChunkValidationError("base_hash must be a non-empty hash string")
    if base_offset is not None and (
        isinstance(base_offset, bool)
        or not isinstance(base_offset, int)
        or base_offset < 0
    ):
        raise ChunkValidationError("base_offset must be a non-negative integer")
    if (base_hash is None) != (base_offset is None):
        raise ChunkValidationError(
            "base_hash and base_offset must be provided together"
        )
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
    force_reprocess: bool = False,
    root: Path = DEFAULT_SPOOL_ROOT,
) -> StagedChunk:
    """Atomically persist one chunk and mark the job ready when complete."""
    chunk_index, total_chunks = _validated_chunk_coordinates(meta, len(chunk_data))
    job_id = _job_id(meta, user_id, device_id)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    with spool_job_lock(job_id, root=root, purpose="stage"):
        completion_path = _completion_path(job_id, root)
        if completion_path.is_file():
            if not force_reprocess:
                return StagedChunk(job_id, complete=True, should_enqueue=False)
            completion_path.unlink()
            _fsync_directory(completion_path.parent)

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
            manifest,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        if manifest_path.exists():
            try:
                existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ChunkValidationError(
                    "existing spool manifest is unreadable"
                ) from exc
            immutable_fields = (
                "job_id",
                "user_id",
                "device_id",
                "device_name",
                "device_platform",
                "total_chunks",
            )
            if any(
                existing.get(field) != manifest.get(field) for field in immutable_fields
            ):
                raise ChunkValidationError(
                    "chunk metadata conflicts with existing upload"
                )
            existing_meta = existing.get("meta", {})
            for field in (
                "upload_id",
                "hash",
                "tool",
                "relative_path",
                "category",
                "content_type",
                "mode",
                "offset",
                "file_size",
                "sync_strategy",
                "metadata",
                "timestamp",
                "base_hash",
                "base_offset",
                "total_chunks",
            ):
                if existing_meta.get(field) != meta.get(field):
                    raise ChunkValidationError(
                        "chunk metadata conflicts with existing upload"
                    )
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
        return StagedChunk(job_id, complete=complete, should_enqueue=complete)


def _ready_candidate_job_ids(
    root: Path,
    *,
    include_failed: bool,
) -> list[str]:
    if not root.exists():
        return []
    return sorted(
        path.name
        for path in root.iterdir()
        if path.is_dir()
        and not path.is_symlink()
        and _JOB_ID_RE.fullmatch(path.name)
        and (path / "ready").is_file()
        and (path / "manifest.json").is_file()
        and not (path / "blocked.json").exists()
        and (include_failed or not (path / "failed.json").exists())
    )


def ready_job_ids(root: Path = DEFAULT_SPOOL_ROOT) -> list[str]:
    """Return safe, durably-ready, non-quarantined job identifiers."""
    return _ready_candidate_job_ids(root, include_failed=False)


def ready_manifest_metadata(
    job_id: str,
    root: Path = DEFAULT_SPOOL_ROOT,
) -> dict[str, Any]:
    """Validate immutable identity/revision metadata without trusting payload."""
    job_dir = _job_dir(job_id, root)
    ready_path = job_dir / "ready"
    manifest_path = job_dir / "manifest.json"
    if (
        not ready_path.is_file()
        or ready_path.is_symlink()
        or not manifest_path.is_file()
        or manifest_path.is_symlink()
    ):
        raise FileNotFoundError(f"spool job {job_id} is not ready")
    if manifest_path.stat().st_size > _MAX_MANIFEST_BYTES:
        raise ChunkValidationError("spool manifest exceeds 1 MiB")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ChunkValidationError("spool manifest is unreadable") from exc
    if not isinstance(manifest, dict) or manifest.get("job_id") != job_id:
        raise ChunkValidationError("spool manifest job id does not match")

    meta = manifest.get("meta")
    if not isinstance(meta, dict):
        raise ChunkValidationError("spool manifest metadata is invalid")
    if any(
        not isinstance(meta.get(field), int) or isinstance(meta.get(field), bool)
        for field in ("chunk_index", "total_chunks", "file_size")
    ):
        raise ChunkValidationError("spool manifest integer metadata is invalid")
    if not isinstance(meta.get("offset", 0), int) or isinstance(
        meta.get("offset", 0), bool
    ):
        raise ChunkValidationError("spool manifest offset is invalid")
    _validated_chunk_coordinates(meta, 0)

    total_chunks = manifest.get("total_chunks")
    if (
        not isinstance(total_chunks, int)
        or isinstance(total_chunks, bool)
        or total_chunks != meta["total_chunks"]
    ):
        raise ChunkValidationError("spool manifest chunk count is invalid")
    for field in ("user_id", "device_id"):
        value = manifest.get(field)
        if not isinstance(value, str) or not value or len(value) > 8192:
            raise ChunkValidationError(f"spool manifest {field} is invalid")
    try:
        if str(UUID(manifest["user_id"])) != manifest["user_id"]:
            raise ValueError
    except (TypeError, ValueError, AttributeError) as exc:
        raise ChunkValidationError("spool manifest user_id is invalid") from exc
    for field in ("device_name", "device_platform"):
        value = manifest.get(field)
        if not isinstance(value, str) or len(value) > 8192:
            raise ChunkValidationError(f"spool manifest {field} is invalid")
    if _job_id(meta, manifest["user_id"], manifest["device_id"]) != job_id:
        raise ChunkValidationError("spool manifest identity does not match job id")
    return manifest


def ready_manifest(
    job_id: str,
    root: Path = DEFAULT_SPOOL_ROOT,
) -> dict[str, Any]:
    """Validate ready metadata plus the complete executable chunk payload."""
    manifest = ready_manifest_metadata(job_id, root)
    job_dir = _job_dir(job_id, root)
    total_chunks = manifest["total_chunks"]

    expected_names = {f"chunk-{index:06d}.bin" for index in range(total_chunks)}
    actual_chunks = {path.name for path in job_dir.glob("chunk-*.bin")}
    if actual_chunks != expected_names:
        raise ChunkValidationError("spool job chunk set is invalid")
    assembled_size = 0
    for name in sorted(expected_names):
        chunk_path = job_dir / name
        if not chunk_path.is_file() or chunk_path.is_symlink():
            raise ChunkValidationError("spool job contains an unsafe chunk")
        assembled_size += chunk_path.stat().st_size
    if assembled_size != manifest["meta"]["file_size"]:
        raise ChunkValidationError("spool chunks do not match declared file_size")
    return manifest


def try_ready_manifest_metadata(
    job_id: str,
    root: Path = DEFAULT_SPOOL_ROOT,
) -> dict[str, Any] | None:
    """Return usable source metadata even when a quarantined payload is corrupt."""
    try:
        return ready_manifest_metadata(job_id, root)
    except (ChunkValidationError, FileNotFoundError, OSError, TypeError, ValueError):
        return None


def try_ready_manifest(
    job_id: str,
    root: Path = DEFAULT_SPOOL_ROOT,
) -> dict[str, Any] | None:
    """Return a validated manifest, excluding malformed candidates from grouping."""
    try:
        return ready_manifest(job_id, root)
    except (ChunkValidationError, FileNotFoundError, OSError, TypeError, ValueError):
        return None


def source_identity(manifest: dict[str, Any]) -> SourceIdentity:
    """Return the user/device/tool/path key that owns ordering and mutation."""
    meta = manifest.get("meta")
    if not isinstance(meta, dict):
        raise ChunkValidationError("spool manifest metadata is invalid")
    identity = (
        manifest.get("user_id"),
        manifest.get("device_id"),
        meta.get("tool"),
        meta.get("relative_path"),
    )
    if any(not isinstance(value, str) or not value for value in identity):
        raise ChunkValidationError("spool source identity is invalid")
    return identity


def _ready_mtime_ns(job_id: str, root: Path) -> int:
    try:
        return (root / job_id / "ready").stat().st_mtime_ns
    except OSError:
        return 0


def _source_sequence_rank(
    job_id: str,
    manifest: dict[str, Any],
    root: Path,
) -> tuple[object, int, int, int, str]:
    """Order mixed FULL/DELTA work without discarding any incremental update."""
    meta = manifest["meta"]
    ready_mtime_ns = _ready_mtime_ns(job_id, root)
    ready_timestamp = normalized_source_timestamp(
        ready_mtime_ns / 1_000_000_000,
    )
    timestamp = bounded_source_timestamp(meta.get("timestamp"), ready_timestamp)
    if timestamp is None:
        timestamp = ready_timestamp
    return (
        timestamp,
        0 if meta.get("mode", "full") == "full" else 1,
        int(meta.get("offset", 0)),
        ready_mtime_ns,
        job_id,
    )


def _full_snapshot_rank(
    job_id: str,
    manifest: dict[str, Any],
    root: Path,
) -> tuple[object, int, int, str, str] | None:
    """Use only revision fields that are persisted in PostgreSQL."""
    meta = manifest["meta"]
    ready_timestamp = normalized_source_timestamp(
        _ready_mtime_ns(job_id, root) / 1_000_000_000,
    )
    revision = full_snapshot_revision(
        timestamp=bounded_source_timestamp(meta.get("timestamp"), ready_timestamp),
        offset=meta.get("offset", 0),
        file_size=meta.get("file_size", 0),
        content_hash=meta.get("hash", ""),
    )
    if revision is None:
        return None
    return (*revision, job_id)


def _ready_jobs_for_source(
    identity: SourceIdentity,
    root: Path,
    *,
    include_failed: bool = False,
) -> list[tuple[str, dict[str, Any]]]:
    jobs: list[tuple[str, dict[str, Any]]] = []
    for candidate_id in _ready_candidate_job_ids(
        root,
        include_failed=include_failed,
    ):
        candidate = try_ready_manifest_metadata(candidate_id, root)
        if candidate is not None and source_identity(candidate) == identity:
            jobs.append((candidate_id, candidate))
    return jobs


def pending_source_revision_job_id(
    *,
    user_id: str,
    device_id: str,
    tool: str,
    relative_path: str,
    content_hash: str,
    offset: int,
    root: Path = DEFAULT_SPOOL_ROOT,
) -> str | None:
    """Return the durable job that will commit a delta's declared base.

    Chunk uploads are acknowledged after fsync and before their Celery job
    commits PostgreSQL.  A guarded delta can therefore legitimately target a
    revision that is present in the durable source queue but not in the
    database yet.  Only non-quarantined ready jobs qualify: a failed revision
    must still force an authoritative full rebase.
    """
    identity: SourceIdentity = (user_id, device_id, tool, relative_path)
    for job_id, manifest in _ready_jobs_for_source(identity, root):
        meta = manifest["meta"]
        if meta.get("hash") == content_hash and int(meta.get("offset", 0)) == int(
            offset
        ):
            head_id, superseded = select_ready_source_head(job_id, root)
            if head_id is not None and job_id not in superseded:
                return job_id
    return None


def select_ready_source_head(
    job_id: str,
    root: Path = DEFAULT_SPOOL_ROOT,
) -> tuple[str | None, tuple[str, ...]]:
    """Select one safe head and its coalescible older FULL cohort.

    FULL snapshots collapse only when every ready job for the source is FULL
    and every snapshot has a persisted deterministic revision. Any DELTA makes
    the source strict FIFO-by-revision and no job is discarded.
    """
    target = ready_manifest_metadata(job_id, root)
    identity = source_identity(target)
    all_jobs = _ready_jobs_for_source(identity, root, include_failed=True)
    jobs = [item for item in all_jobs if not (root / item[0] / "failed.json").exists()]
    if not jobs:
        return None, ()

    all_full = all(
        manifest["meta"].get("mode", "full") == "full" for _, manifest in all_jobs
    )
    full_ranks = {
        candidate_id: _full_snapshot_rank(candidate_id, manifest, root)
        for candidate_id, manifest in all_jobs
    }
    if all_full and all(rank is not None for rank in full_ranks.values()):
        head_id = max(jobs, key=lambda item: full_ranks[item[0]])[0]
        head_rank = full_ranks[head_id]
        cohort = tuple(
            sorted(
                candidate_id
                for candidate_id, _ in all_jobs
                if candidate_id != head_id
                and (
                    not (root / candidate_id / "failed.json").exists()
                    or full_ranks[candidate_id] <= head_rank
                )
            )
        )
        return head_id, cohort

    ordered = sorted(
        all_jobs,
        key=lambda item: _source_sequence_rank(item[0], item[1], root),
    )
    failed_indexes = [
        index
        for index, (candidate_id, _) in enumerate(ordered)
        if (root / candidate_id / "failed.json").exists()
    ]
    if failed_indexes:
        barrier_index = failed_indexes[0]
        for candidate_id, _ in ordered[:barrier_index]:
            if not (root / candidate_id / "failed.json").exists():
                return candidate_id, ()

        for rebase_index in range(barrier_index + 1, len(ordered)):
            candidate_id, manifest = ordered[rebase_index]
            if (root / candidate_id / "failed.json").exists():
                continue
            if manifest["meta"].get("mode", "full") != "full":
                continue
            superseded = tuple(
                sorted(previous_id for previous_id, _ in ordered[:rebase_index])
            )
            return candidate_id, superseded
        return None, ()

    return ordered[0][0], ()


def ready_job_ids_in_recovery_order(
    root: Path = DEFAULT_SPOOL_ROOT,
) -> list[str]:
    """Return one actionable source head plus malformed jobs for quarantine."""
    heads: dict[SourceIdentity, str] = {}
    malformed: list[str] = []
    for job_id in ready_job_ids(root):
        manifest = try_ready_manifest_metadata(job_id, root)
        if manifest is None:
            malformed.append(job_id)
            continue
        identity = source_identity(manifest)
        if identity in heads:
            continue
        head_id = select_ready_source_head(job_id, root)[0]
        if head_id is not None:
            heads[identity] = head_id
    return sorted(malformed) + [heads[key] for key in sorted(heads)]


def next_ready_source_head(
    identity: SourceIdentity,
    root: Path = DEFAULT_SPOOL_ROOT,
) -> str | None:
    """Return the currently actionable job for a source after state changes."""
    jobs = _ready_jobs_for_source(identity, root)
    if not jobs:
        return None
    return select_ready_source_head(jobs[0][0], root)[0]


def superseding_ready_full_job_id(
    job_id: str,
    root: Path = DEFAULT_SPOOL_ROOT,
) -> str | None:
    """Compatibility helper returning the all-FULL source head, if newer."""
    try:
        head_id, cohort = select_ready_source_head(job_id, root)
    except (ChunkValidationError, FileNotFoundError):
        return None
    return head_id if head_id is not None and job_id in cohort else None


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


def blocked_job_ids(root: Path = DEFAULT_SPOOL_ROOT) -> list[str]:
    """Return retained jobs superseded by an authoritative FULL rebase."""
    if not root.exists():
        return []
    return sorted(
        path.name
        for path in root.iterdir()
        if path.is_dir()
        and _JOB_ID_RE.fullmatch(path.name)
        and (path / "blocked.json").is_file()
    )


def assemble_job(
    job_id: str,
    root: Path = DEFAULT_SPOOL_ROOT,
    *,
    manifest: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], Path]:
    """Assemble a ready job to disk without retaining all chunks in memory."""
    job_dir = _job_dir(job_id, root)
    manifest = ready_manifest(job_id, root) if manifest is None else manifest
    if manifest.get("job_id") != job_id:
        raise ChunkValidationError("spool manifest job id does not match")
    total_chunks = manifest["total_chunks"]
    expected_size = manifest["meta"]["file_size"]
    payload_path = job_dir / "payload.bin"
    if not payload_path.exists():
        temporary = job_dir / f".payload.{uuid.uuid4().hex}.tmp"
        try:
            with _filesystem_lock(root / ".quota.lock"):
                _assert_spool_capacity(root, expected_size)
                with temporary.open("wb") as output:
                    for index in range(total_chunks):
                        chunk_path = job_dir / f"chunk-{index:06d}.bin"
                        if not chunk_path.is_file() or chunk_path.is_symlink():
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


def complete_and_remove_job(
    job_id: str,
    *,
    document_id: str,
    root: Path = DEFAULT_SPOOL_ROOT,
) -> bool:
    """Atomically receipt then remove one committed job under its stage lock."""
    with spool_job_lock(job_id, root=root, purpose="stage"):
        job_dir = _job_dir(job_id, root)
        if not job_dir.is_dir():
            return False
        mark_job_complete(job_id, document_id=document_id, root=root)
        shutil.rmtree(job_dir, ignore_errors=True)
        _fsync_directory(root)
        return True


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


def mark_job_blocked(
    job_id: str,
    *,
    superseding_job_id: str,
    document_id: str,
    root: Path = DEFAULT_SPOOL_ROOT,
) -> None:
    """Retain evidence for a broken DELTA chain superseded by a later FULL."""
    if not _JOB_ID_RE.fullmatch(superseding_job_id):
        raise ChunkValidationError("invalid superseding spool job id")
    payload = json.dumps(
        {
            "job_id": job_id,
            "reason": "superseded_by_full_rebase",
            "superseding_job_id": superseding_job_id,
            "document_id": document_id,
            "blocked_at": time.time(),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    with spool_job_lock(job_id, root=root, purpose="stage"):
        job_dir = _job_dir(job_id, root)
        if not job_dir.is_dir():
            raise FileNotFoundError(f"spool job {job_id} is missing")
        _atomic_write(job_dir / "blocked.json", payload)


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
                    int(
                        json.loads(attempt_path.read_text(encoding="utf-8"))["attempts"]
                    ),
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
