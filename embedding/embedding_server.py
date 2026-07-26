"""Standalone embedding HTTP server — Docker image and native host entrypoint.

Usage:
  python embedding_server.py [--port 8002] [--model BAAI/bge-m3]
  python -m server.services.embedding_server [--port 8002] [--model BAAI/bge-m3]

Provides a single endpoint:
  POST /embed  {"texts": ["hello", "world"]}  →  {"embeddings": [[...], [...]]}

Runtime configuration (feature-flagged; defaults preserve torch/CPU-Docker behavior):
  MEMENTO_EMBEDDING_BACKEND=torch|onnx          (default: torch)
  MEMENTO_EMBEDDING_DEVICE=auto|cpu|cuda|mps    (default: auto)
  MEMENTO_EMBEDDING_ONNX_PROVIDER=...           (default: CPUExecutionProvider for onnx)
  MEMENTO_EMBEDDING_ONNX_FILE=...               (optional ONNX weight filename)

Device policy for auto:
  - Prefer CUDA when usable.
  - On macOS, keep the documented stable CPU path (do not auto-select MPS).
Forced cuda/mps is rejected clearly when unavailable/unsupported.
ONNX is CPU-only in this product path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import platform
import socket
import threading
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any


class DualStackHTTPServer(ThreadingMixIn, HTTPServer):
    """Listen on both IPv4 and IPv6. Without this, the default HTTPServer
    binds AF_INET only, but docker DNS for a service alias returns AAAA
    records first — clients that follow RFC 6555 happy-eyeballs spend
    seconds timing out the IPv6 attempt before falling back to IPv4.
    Binding to `::` with IPV6_V6ONLY=0 means the same socket accepts
    both v4 and v6 traffic, no client-side workaround needed."""

    address_family = socket.AF_INET6
    daemon_threads = True

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("embedding_server")

_model = None
_encode_lock = threading.Lock()
_embed_slots = threading.BoundedSemaphore(
    int(os.environ.get("MEMENTO_EMBEDDING_MAX_INFLIGHT_REQUESTS", "1"))
)
_max_request_bytes = int(
    os.environ.get("MEMENTO_EMBEDDING_MAX_REQUEST_BYTES", str(8 * 1024 * 1024))
)
_model_batch_size = int(os.environ.get("MEMENTO_EMBEDDING_MODEL_BATCH_SIZE", "10"))
# BGE-M3 supports up to 8192 tokens. Roughly cap text at 32000 chars (~8k tokens).
MAX_TEXT_CHARS = 32000

_VALID_BACKENDS = frozenset({"torch", "onnx"})
_VALID_DEVICES = frozenset({"auto", "cpu", "cuda", "mps"})
_VALID_PURPOSES = frozenset({"query", "document"})
_PROFILE_VERSION = 1


@dataclass(frozen=True)
class RuntimeInfo:
    model_name: str
    backend: str
    device: str
    provider: str | None
    dimension: int | None
    onnx_file: str | None = None
    model_revision: str = ""
    query_prefix: str = ""
    document_prefix: str = ""
    max_sequence_length: int = 0
    artifact_sha256: str = ""
    profile_signature: str = ""
    providers: tuple[str, ...] = ()
    cpu_threads: int = 1

    def health_payload(self, *, model_loaded: bool) -> dict[str, Any]:
        return {
            "status": "ok",
            "model": model_loaded,
            "model_name": self.model_name,
            "backend": self.backend,
            "device": self.device,
            "provider": self.provider,
            "providers": list(self.providers),
            "dimension": self.dimension,
            "onnx_file": self.onnx_file,
            "model_revision": self.model_revision,
            "max_sequence_length": self.max_sequence_length,
            "artifact_sha256": self.artifact_sha256 or None,
            "profile_signature": self.profile_signature or None,
            "cpu_threads": self.cpu_threads,
        }


_runtime: RuntimeInfo | None = None


def request_runtime_mismatch(
    body: dict[str, Any],
    runtime: RuntimeInfo | None,
) -> str | None:
    """Return a reason when a client requests a different loaded profile."""
    if runtime is None:
        return None
    requested_model = body.get("model")
    if requested_model is not None and requested_model != runtime.model_name:
        return (
            f"requested model {requested_model!r} does not match loaded "
            f"model {runtime.model_name!r}"
        )
    requested_dimensions = body.get("dimensions")
    if (
        requested_dimensions is not None
        and requested_dimensions != runtime.dimension
    ):
        return (
            f"requested dimensions {requested_dimensions!r} do not match "
            f"loaded dimensions {runtime.dimension!r}"
        )
    requested_backend = body.get("backend")
    if (
        requested_backend is not None
        and requested_backend != runtime.backend
    ):
        return (
            f"requested backend {requested_backend!r} does not match loaded "
            f"backend {runtime.backend!r}"
        )
    requested_profile = body.get("profile_signature")
    if (
        requested_profile is not None
        and requested_profile != runtime.profile_signature
    ):
        return (
            f"requested profile_signature {requested_profile!r} does not match "
            f"loaded profile_signature {runtime.profile_signature!r}"
        )
    return None


def resolve_backend(raw: str | None = None) -> str:
    value = (raw if raw is not None else os.environ.get("MEMENTO_EMBEDDING_BACKEND", "torch"))
    backend = (value or "torch").strip().lower() or "torch"
    if backend not in _VALID_BACKENDS:
        raise ValueError(
            f"Invalid MEMENTO_EMBEDDING_BACKEND={value!r}; expected torch|onnx"
        )
    return backend


def resolve_onnx_file(raw: str | None = None) -> str | None:
    value = (
        raw
        if raw is not None
        else os.environ.get("MEMENTO_EMBEDDING_ONNX_FILE", "")
    )
    file_name = (value or "").strip()
    return file_name or None


def resolve_onnx_provider(
    raw: str | None = None,
    *,
    backend: str,
    available_providers: list[str] | tuple[str, ...] | None = None,
) -> str | None:
    if backend != "onnx":
        return None
    value = (
        raw
        if raw is not None
        else os.environ.get("MEMENTO_EMBEDDING_ONNX_PROVIDER", "")
    )
    provider = (value or "").strip()
    provider = provider or "CPUExecutionProvider"
    if available_providers is None:
        try:
            import onnxruntime as ort

            available_providers = ort.get_available_providers()
        except Exception as exc:
            raise ValueError(
                "ONNX Runtime is unavailable while resolving the execution provider"
            ) from exc
    if provider not in available_providers:
        raise ValueError(
            f"MEMENTO_EMBEDDING_ONNX_PROVIDER={provider!r} is unavailable; "
            f"installed providers: {list(available_providers)!r}"
        )
    return provider


def resolve_cpu_threads(raw: str | None = None) -> int:
    value = (
        raw
        if raw is not None
        else os.environ.get("MEMENTO_EMBEDDING_CPU_THREADS", "1")
    )
    try:
        threads = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Invalid MEMENTO_EMBEDDING_CPU_THREADS={value!r}; expected 1..64"
        ) from exc
    if not 1 <= threads <= 64:
        raise ValueError(
            f"Invalid MEMENTO_EMBEDDING_CPU_THREADS={value!r}; expected 1..64"
        )
    return threads


def embedding_profile_signature(
    *,
    model_name: str,
    model_revision: str,
    backend: str,
    dimension: int,
    query_prefix: str,
    document_prefix: str,
    max_sequence_length: int,
    onnx_file: str,
    artifact_sha256: str,
) -> str:
    """Return the immutable identity of one compatible vector space."""
    payload = {
        "artifact_sha256": artifact_sha256,
        "backend": backend,
        "dimension": dimension,
        "document_prefix": document_prefix,
        "max_sequence_length": max_sequence_length,
        "model_name": model_name,
        "model_revision": model_revision,
        "normalization": "l2",
        "onnx_file": onnx_file,
        "pooling": "sentence-transformers-config",
        "profile_version": _PROFILE_VERSION,
        "query_prefix": query_prefix,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _resolve_artifact_sha256(
    model_name: str,
    onnx_file: str | None,
    configured: str | None = None,
) -> str:
    value = (
        configured
        if configured is not None
        else os.environ.get("MEMENTO_EMBEDDING_ARTIFACT_SHA256", "")
    )
    checksum = (value or "").strip().lower()
    candidate = Path(model_name) / onnx_file if onnx_file else None
    if checksum:
        if len(checksum) != 64 or any(c not in "0123456789abcdef" for c in checksum):
            raise ValueError(
                "MEMENTO_EMBEDDING_ARTIFACT_SHA256 must be 64 lowercase hex characters"
            )
        if candidate is not None and candidate.is_file():
            digest = hashlib.sha256()
            with candidate.open("rb") as artifact:
                for block in iter(lambda: artifact.read(1024 * 1024), b""):
                    digest.update(block)
            actual = digest.hexdigest()
            if actual != checksum:
                raise ValueError(
                    f"ONNX artifact checksum mismatch for {candidate}: "
                    f"got {actual}, expected {checksum}"
                )
        return checksum
    if not onnx_file:
        return ""
    if candidate is None or not candidate.is_file():
        raise ValueError(
            "ONNX deployments require MEMENTO_EMBEDDING_ARTIFACT_SHA256 when "
            f"the artifact cannot be hashed locally ({candidate})"
        )
    digest = hashlib.sha256()
    with candidate.open("rb") as artifact:
        for block in iter(lambda: artifact.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _actual_onnx_providers(model: Any) -> tuple[str, ...]:
    """Find the InferenceSession without depending on one Optimum release."""
    candidates: list[Any] = [model]
    try:
        candidates.append(model[0])
    except Exception:
        pass
    seen: set[int] = set()
    while candidates:
        candidate = candidates.pop(0)
        identity = id(candidate)
        if identity in seen:
            continue
        seen.add(identity)
        getter = getattr(candidate, "get_providers", None)
        if callable(getter):
            return tuple(str(item) for item in getter())
        for attribute in ("auto_model", "model", "session"):
            nested = getattr(candidate, attribute, None)
            if nested is not None:
                candidates.append(nested)
    raise RuntimeError("Could not verify the ONNX Runtime session providers")


def _prepare_texts(
    texts: list[str],
    *,
    purpose: str,
    runtime: RuntimeInfo,
) -> list[str]:
    if purpose not in _VALID_PURPOSES:
        raise ValueError("purpose must be 'query' or 'document'")
    prefix = runtime.query_prefix if purpose == "query" else runtime.document_prefix
    return [prefix + text[:MAX_TEXT_CHARS] for text in texts]


def validate_embedding_vectors(
    embeddings: Any,
    *,
    expected_count: int,
    dimension: int,
) -> list[list[float]]:
    """Reject malformed, non-finite, or non-normalized model output."""
    vectors = [vector.tolist() if hasattr(vector, "tolist") else list(vector) for vector in embeddings]
    if len(vectors) != expected_count:
        raise ValueError(
            f"embedding count mismatch: got {len(vectors)}, expected {expected_count}"
        )
    for index, vector in enumerate(vectors):
        if len(vector) != dimension:
            raise ValueError(
                f"embedding {index} dimension mismatch: got {len(vector)}, "
                f"expected {dimension}"
            )
        if any(not math.isfinite(float(value)) for value in vector):
            raise ValueError(f"embedding {index} contains non-finite values")
        norm = math.sqrt(sum(float(value) ** 2 for value in vector))
        if not math.isfinite(norm) or abs(norm - 1.0) > 1e-3:
            raise ValueError(
                f"embedding {index} is not normalized (L2 norm {norm:.6f})"
            )
    return vectors


def cuda_is_usable() -> bool:
    try:
        import torch

        return bool(torch.cuda.is_available())
    except Exception:
        return False


def mps_is_usable() -> bool:
    try:
        import torch

        mps = getattr(torch.backends, "mps", None)
        return bool(mps is not None and mps.is_available())
    except Exception:
        return False


def resolve_device(
    raw: str | None = None,
    *,
    backend: str,
    cuda_usable: bool | None = None,
    mps_usable: bool | None = None,
    system: str | None = None,
) -> str:
    """Resolve MEMENTO_EMBEDDING_DEVICE with explicit rejection of bad GPU forces.

    auto:
      - CUDA when usable
      - otherwise CPU (including on macOS — do not auto-select MPS)
    """
    value = (
        raw if raw is not None else os.environ.get("MEMENTO_EMBEDDING_DEVICE", "auto")
    )
    requested = (value or "auto").strip().lower() or "auto"
    if requested not in _VALID_DEVICES:
        raise ValueError(
            f"Invalid MEMENTO_EMBEDDING_DEVICE={value!r}; expected auto|cpu|cuda|mps"
        )

    if backend == "onnx":
        if requested in ("cuda", "mps"):
            raise ValueError(
                f"MEMENTO_EMBEDDING_BACKEND=onnx does not support "
                f"MEMENTO_EMBEDDING_DEVICE={requested}; use cpu or auto"
            )
        return "cpu"

    sys_name = system if system is not None else platform.system()
    cuda_ok = cuda_is_usable() if cuda_usable is None else cuda_usable
    mps_ok = mps_is_usable() if mps_usable is None else mps_usable

    if requested == "cpu":
        return "cpu"
    if requested == "cuda":
        if not cuda_ok:
            raise ValueError(
                "MEMENTO_EMBEDDING_DEVICE=cuda requested but CUDA is not available"
            )
        return "cuda"
    if requested == "mps":
        if sys_name != "Darwin":
            raise ValueError(
                "MEMENTO_EMBEDDING_DEVICE=mps is only supported on macOS (Darwin)"
            )
        if not mps_ok:
            raise ValueError(
                "MEMENTO_EMBEDDING_DEVICE=mps requested but MPS is not available"
            )
        return "mps"

    # auto
    if cuda_ok:
        return "cuda"
    return "cpu"


def build_runtime_info(
    model_name: str,
    *,
    backend: str | None = None,
    device: str | None = None,
    provider: str | None = None,
    onnx_file: str | None = None,
    dimension: int | None = None,
    model_revision: str | None = None,
    query_prefix: str | None = None,
    document_prefix: str | None = None,
    max_sequence_length: int | None = None,
    artifact_sha256: str | None = None,
    profile_signature: str | None = None,
    providers: tuple[str, ...] = (),
    cpu_threads: int | None = None,
    available_onnx_providers: list[str] | tuple[str, ...] | None = None,
    cuda_usable: bool | None = None,
    mps_usable: bool | None = None,
    system: str | None = None,
) -> RuntimeInfo:
    resolved_backend = resolve_backend(backend)
    resolved_device = resolve_device(
        device,
        backend=resolved_backend,
        cuda_usable=cuda_usable,
        mps_usable=mps_usable,
        system=system,
    )
    resolved_file = resolve_onnx_file(onnx_file) if resolved_backend == "onnx" else None
    # Allow explicit None to mean "resolve from env"; empty string clears.
    if provider is None and resolved_backend == "onnx":
        resolved_provider = resolve_onnx_provider(
            None,
            backend=resolved_backend,
            available_providers=available_onnx_providers,
        )
    elif resolved_backend != "onnx":
        resolved_provider = None
    else:
        resolved_provider = resolve_onnx_provider(
            provider,
            backend=resolved_backend,
            available_providers=available_onnx_providers,
        )
    revision = (
        model_revision
        if model_revision is not None
        else os.environ.get("MEMENTO_EMBEDDING_MODEL_REVISION", "")
    ).strip()
    query_text_prefix = (
        query_prefix
        if query_prefix is not None
        else os.environ.get("MEMENTO_EMBEDDING_QUERY_PREFIX", "")
    )
    document_text_prefix = (
        document_prefix
        if document_prefix is not None
        else os.environ.get("MEMENTO_EMBEDDING_DOCUMENT_PREFIX", "")
    )
    if max_sequence_length is None:
        raw_max_length = os.environ.get("MEMENTO_EMBEDDING_MAX_SEQUENCE_LENGTH", "0")
        try:
            resolved_max_length = int(raw_max_length)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "MEMENTO_EMBEDDING_MAX_SEQUENCE_LENGTH must be a non-negative integer"
            ) from exc
    else:
        resolved_max_length = max_sequence_length
    if resolved_max_length < 0:
        raise ValueError(
            "MEMENTO_EMBEDDING_MAX_SEQUENCE_LENGTH must be a non-negative integer"
        )
    resolved_threads = resolve_cpu_threads(
        str(cpu_threads) if cpu_threads is not None else None
    )
    resolved_artifact = (artifact_sha256 or "").strip().lower()
    resolved_signature = (profile_signature or "").strip()
    if not resolved_signature and dimension is not None:
        resolved_signature = embedding_profile_signature(
            model_name=model_name,
            model_revision=revision,
            backend=resolved_backend,
            dimension=dimension,
            query_prefix=query_text_prefix,
            document_prefix=document_text_prefix,
            max_sequence_length=resolved_max_length,
            onnx_file=resolved_file or "",
            artifact_sha256=(
                resolved_artifact if resolved_backend == "onnx" else ""
            ),
        )
    return RuntimeInfo(
        model_name=model_name,
        backend=resolved_backend,
        device=resolved_device,
        provider=resolved_provider,
        dimension=dimension,
        onnx_file=resolved_file,
        model_revision=revision,
        query_prefix=query_text_prefix,
        document_prefix=document_text_prefix,
        max_sequence_length=resolved_max_length,
        artifact_sha256=resolved_artifact,
        profile_signature=resolved_signature,
        providers=providers,
        cpu_threads=resolved_threads,
    )


def _load_model(model_name: str) -> RuntimeInfo:
    global _model, _runtime
    # Docker may load from a materialized ModelScope/ONNX artifact path while
    # clients identify the logical model configured at build/deploy time.
    model_identity = os.environ.get(
        "MEMENTO_EMBEDDING_MODEL_IDENTITY",
        model_name,
    ).strip() or model_name
    runtime = build_runtime_info(model_identity)
    artifact_sha256 = (
        _resolve_artifact_sha256(model_name, runtime.onnx_file)
        if runtime.backend == "onnx"
        else ""
    )
    logger.info(
        "Loading %s (backend=%s device=%s provider=%s onnx_file=%s) ...",
        runtime.model_name,
        runtime.backend,
        runtime.device,
        runtime.provider,
        runtime.onnx_file,
    )
    from sentence_transformers import SentenceTransformer

    revision_kwargs: dict[str, Any] = {}
    if runtime.model_revision:
        revision_kwargs["revision"] = runtime.model_revision
    if runtime.backend == "onnx":
        import onnxruntime as ort

        session_options = ort.SessionOptions()
        session_options.intra_op_num_threads = runtime.cpu_threads
        session_options.inter_op_num_threads = 1
        session_options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        model_kwargs: dict[str, Any] = {
            "provider": runtime.provider,
            "session_options": session_options,
        }
        if runtime.onnx_file:
            model_kwargs["file_name"] = runtime.onnx_file
        _model = SentenceTransformer(
            model_name,
            backend="onnx",
            device="cpu",
            model_kwargs=model_kwargs,
            **revision_kwargs,
        )
        actual_providers = _actual_onnx_providers(_model)
        if runtime.provider not in actual_providers:
            raise RuntimeError(
                f"ONNX Runtime loaded providers {actual_providers!r}; "
                f"requested {runtime.provider!r}"
            )
    else:
        _model = SentenceTransformer(
            model_name,
            device=runtime.device,
            **revision_kwargs,
        )
        actual_providers = ()

    dimension = int(_model.get_sentence_embedding_dimension())
    if runtime.max_sequence_length:
        _model.max_seq_length = runtime.max_sequence_length
    signature = embedding_profile_signature(
        model_name=runtime.model_name,
        model_revision=runtime.model_revision,
        backend=runtime.backend,
        dimension=dimension,
        query_prefix=runtime.query_prefix,
        document_prefix=runtime.document_prefix,
        max_sequence_length=runtime.max_sequence_length,
        onnx_file=runtime.onnx_file or "",
        artifact_sha256=artifact_sha256,
    )
    runtime = RuntimeInfo(
        model_name=runtime.model_name,
        backend=runtime.backend,
        device=runtime.device,
        provider=runtime.provider,
        dimension=dimension,
        onnx_file=runtime.onnx_file,
        model_revision=runtime.model_revision,
        query_prefix=runtime.query_prefix,
        document_prefix=runtime.document_prefix,
        max_sequence_length=runtime.max_sequence_length,
        artifact_sha256=artifact_sha256,
        profile_signature=signature,
        providers=actual_providers,
        cpu_threads=runtime.cpu_threads,
    )
    probe_texts = _prepare_texts(
        ["memento embedding readiness probe"],
        purpose="document",
        runtime=runtime,
    )
    probe = _model.encode(
        probe_texts,
        batch_size=1,
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    validate_embedding_vectors(probe, expected_count=1, dimension=dimension)
    _runtime = runtime
    logger.info(
        "Model loaded: %s (dim=%d backend=%s device=%s provider=%s)",
        runtime.model_name,
        dimension,
        runtime.backend,
        runtime.device,
        runtime.provider,
    )
    return runtime


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/embed":
            self._safe_error(404)
            return
        if not _embed_slots.acquire(blocking=False):
            # Do not queue behind a multi-minute CPU inference. HTTP clients
            # can time out while waiting, leaving the server to perform stale
            # work after nobody is listening. Callers persist a failed status
            # and retry later, so an immediate 503 is both cheaper and safer.
            self._safe_error(503, "embedding server is busy")
            return
        try:
            try:
                length = int(self.headers.get("Content-Length", 0))
            except (TypeError, ValueError):
                self._safe_error(400, "invalid Content-Length")
                return
            if length < 0 or length > _max_request_bytes:
                self._safe_error(413, "embedding request is too large")
                return
            try:
                body = json.loads(self.rfile.read(length))
            except json.JSONDecodeError:
                self._safe_error(400, "invalid JSON body")
                return
            if not isinstance(body, dict):
                self._safe_error(400, "JSON body must be an object")
                return
            mismatch = request_runtime_mismatch(body, _runtime)
            if mismatch is not None:
                self._safe_error(409, mismatch)
                return
            texts = body.get("texts", [])
            if not texts:
                self._json_response(
                    {
                        "embeddings": [],
                        "profile_signature": (
                            _runtime.profile_signature if _runtime else None
                        ),
                    }
                )
                return
            if not isinstance(texts, list) or any(
                not isinstance(text, str) for text in texts
            ):
                self._safe_error(400, "texts must be a list of strings")
                return
            purpose = body.get("purpose", "document")
            if purpose not in _VALID_PURPOSES:
                self._safe_error(400, "purpose must be 'query' or 'document'")
                return

            # Defensive: clip oversized inputs to avoid tokenizer / GPU crashes
            if _runtime is not None:
                texts = _prepare_texts(texts, purpose=purpose, runtime=_runtime)
            else:
                # Unit-test and embedded compatibility path. Production always
                # has runtime metadata because main() loads the model first.
                texts = [text[:MAX_TEXT_CHARS] for text in texts]

            # SentenceTransformer inference remains serialized, while the
            # threaded HTTP server can still answer /health during a long run.
            with _encode_lock:
                embeddings = _model.encode(
                    texts,
                    batch_size=_model_batch_size,
                    normalize_embeddings=True,
                    show_progress_bar=False,
                )
            expected_dimension = (
                _runtime.dimension
                if _runtime is not None and _runtime.dimension is not None
                else len(
                    embeddings[0].tolist()
                    if hasattr(embeddings[0], "tolist")
                    else embeddings[0]
                )
            )
            vectors = validate_embedding_vectors(
                embeddings,
                expected_count=len(texts),
                dimension=expected_dimension,
            )
            self._json_response(
                {
                    "embeddings": vectors,
                    "profile_signature": (
                        _runtime.profile_signature if _runtime else None
                    ),
                }
            )
        except ConnectionError:
            logger.info("Embedding client disconnected before the response completed")
        except Exception as e:
            logger.error("Error: %s", e)
            self._safe_error(500, str(e))
        finally:
            _embed_slots.release()

    def do_GET(self):
        if self.path == "/health":
            # Before _load_model completes (and in unit tests that only stub
            # `_model`), keep the historical compact payload. After load,
            # expose selected model/backend/device/provider/dimension.
            if _runtime is not None:
                payload = _runtime.health_payload(model_loaded=_model is not None)
            else:
                payload = {"status": "ok", "model": _model is not None}
            self._json_response(payload)
        else:
            self._safe_error(404)

    def _safe_error(self, code, message=None):
        try:
            self.send_error(code, message)
        except (BrokenPipeError, ConnectionResetError, OSError):
            logger.info("HTTP client disconnected before error response completed")

    def _json_response(self, data):
        body = json.dumps(data).encode()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            logger.info("HTTP client disconnected before the response completed")

    def log_message(self, format, *args):
        logger.info(format, *args)


def main():
    parser = argparse.ArgumentParser(description="Embedding HTTP Server")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MEMENTO_EMBEDDING_PORT", "8002")),
    )
    parser.add_argument(
        "--model", default=os.environ.get("MEMENTO_EMBEDDING_MODEL_NAME", "BAAI/bge-m3")
    )
    args = parser.parse_args()

    _load_model(args.model)

    DualStackHTTPServer.allow_reuse_address = True
    server = DualStackHTTPServer(("::", args.port), Handler)
    logger.info(
        "Embedding server running on port %d (dual-stack) runtime=%s",
        args.port,
        asdict(_runtime) if _runtime else None,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
