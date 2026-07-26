"""Compare embedding backends without external Python dependencies.

Examples:
  python3 tools/benchmark_embedding_backends.py \
    --reference http://172.18.0.5:8002 \
    --candidate http://127.0.0.1:8003 \
    --reference-dim 1024 --candidate-dim 1024 --min-vector-cosine 0.97

The tool warms each backend, reports repeated p50/p95 latency and throughput,
and keeps query/document requests separate so asymmetric models receive the
same purpose prefixes they use in production. For a different-dimensional
fast model, omit --min-vector-cosine and use the ranking-overlap gate.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import time
import urllib.request


QUERIES = [
    "How do I reduce PostgreSQL dead tuples during continuous ingestion?",
    "How should an embedding worker avoid recomputing unchanged chunks?",
    "如何降低 Windows Defender 扫描开发目录造成的 CPU 占用？",
    "How can GPU embedding fall back safely when CUDA is unavailable?",
]

DOCUMENTS = [
    "Use lower table-specific autovacuum scale factors for append-heavy PostgreSQL tables.",
    "A larger shared buffer is unrelated to antivirus exclusions on Windows.",
    "Store a content hash per chunk and only upsert vectors whose hash changed.",
    "Rebuild every vector and HNSW entry whenever a transcript receives one new line.",
    "为高频变化的项目目录添加 Defender 排除项可以减少实时扫描开销。",
    "Windows Delivery Optimization can trigger repeated WMI network-stat queries.",
    "Validate torch.cuda.is_available before accepting a forced CUDA device.",
    "When CUDA is absent, select the CPU backend and expose that choice in health metadata.",
    "Keep 384-dimensional and 1024-dimensional vectors in separate pgvector indexes.",
    "Reciprocal-rank fusion combines rankings without comparing incompatible cosine scores.",
    "Checkpoint smoothing can reduce write spikes but does not change embedding dimensions.",
    "A read-only search replica should never receive embedding write transactions.",
]


def _embed(
    endpoint: str,
    texts: list[str],
    *,
    purpose: str,
) -> tuple[list[list[float]], float]:
    payload = json.dumps(
        {"texts": texts, "purpose": purpose},
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{endpoint.rstrip('/')}/embed",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=1200) as response:
        body = json.load(response)
    elapsed = time.perf_counter() - started
    return body["embeddings"], elapsed


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def _measure(
    endpoint: str,
    *,
    warmups: int,
    repetitions: int,
) -> dict:
    for _ in range(warmups):
        _embed(endpoint, [QUERIES[0]], purpose="query")
        _embed(endpoint, [DOCUMENTS[0]], purpose="document")

    query_samples: list[float] = []
    document_samples: list[float] = []
    query_vectors: list[list[float]] = []
    document_vectors: list[list[float]] = []
    for _ in range(repetitions):
        query_vectors, query_seconds = _embed(
            endpoint,
            QUERIES,
            purpose="query",
        )
        document_vectors, document_seconds = _embed(
            endpoint,
            DOCUMENTS,
            purpose="document",
        )
        query_samples.append(query_seconds)
        document_samples.append(document_seconds)

    total_texts = repetitions * len(DOCUMENTS)
    total_document_seconds = sum(document_samples)
    return {
        "query_vectors": query_vectors,
        "document_vectors": document_vectors,
        "query_p50_seconds": statistics.median(query_samples),
        "query_p95_seconds": _percentile(query_samples, 0.95),
        "document_p50_seconds": statistics.median(document_samples),
        "document_p95_seconds": _percentile(document_samples, 0.95),
        "document_texts_per_second": (
            total_texts / total_document_seconds
            if total_document_seconds
            else None
        ),
    }


def _dot(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


def _norm(vector: list[float]) -> float:
    return math.sqrt(_dot(vector, vector))


def _cosine(left: list[float], right: list[float]) -> float:
    denom = _norm(left) * _norm(right)
    return _dot(left, right) / denom if denom else 0.0


def _rankings(
    query_vectors: list[list[float]],
    document_vectors: list[list[float]],
    *,
    top_k: int,
) -> list[list[int]]:
    return [
        sorted(
            range(len(document_vectors)),
            key=lambda index: _cosine(query, document_vectors[index]),
            reverse=True,
        )[:top_k]
        for query in query_vectors
    ]


def _mean_topk_overlap(
    reference: list[list[int]],
    candidate: list[list[int]],
) -> float:
    overlaps = []
    for left, right in zip(reference, candidate):
        union = set(left) | set(right)
        overlaps.append(len(set(left) & set(right)) / len(union) if union else 1.0)
    return statistics.mean(overlaps)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--reference-dim", type=int, required=True)
    parser.add_argument("--candidate-dim", type=int, required=True)
    parser.add_argument("--top-k", type=int, default=4)
    parser.add_argument("--min-topk-overlap", type=float, default=0.65)
    parser.add_argument("--min-vector-cosine", type=float)
    parser.add_argument("--min-speedup", type=float)
    parser.add_argument("--max-candidate-query-p95", type=float)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--repetitions", type=int, default=5)
    args = parser.parse_args()

    if args.warmups < 0 or args.repetitions < 1:
        parser.error("--warmups must be >= 0 and --repetitions must be >= 1")

    reference_run = _measure(
        args.reference,
        warmups=args.warmups,
        repetitions=args.repetitions,
    )
    candidate_run = _measure(
        args.candidate,
        warmups=args.warmups,
        repetitions=args.repetitions,
    )
    reference_queries = reference_run["query_vectors"]
    reference_documents = reference_run["document_vectors"]
    candidate_queries = candidate_run["query_vectors"]
    candidate_documents = candidate_run["document_vectors"]
    reference = reference_queries + reference_documents
    candidate = candidate_queries + candidate_documents

    failures: list[str] = []
    if any(len(vector) != args.reference_dim for vector in reference):
        failures.append("reference dimension mismatch")
    if any(len(vector) != args.candidate_dim for vector in candidate):
        failures.append("candidate dimension mismatch")

    reference_norm_error = max(abs(_norm(vector) - 1.0) for vector in reference)
    candidate_norm_error = max(abs(_norm(vector) - 1.0) for vector in candidate)
    if reference_norm_error >= 1e-3:
        failures.append("reference vectors are not normalized")
    if candidate_norm_error >= 1e-3:
        failures.append("candidate vectors are not normalized")

    topk_overlap = _mean_topk_overlap(
        _rankings(
            reference_queries,
            reference_documents,
            top_k=args.top_k,
        ),
        _rankings(
            candidate_queries,
            candidate_documents,
            top_k=args.top_k,
        ),
    )
    if topk_overlap < args.min_topk_overlap:
        failures.append("candidate top-k ranking overlap below threshold")

    vector_cosines = None
    mixed_query_overlap = None
    mixed_document_overlap = None
    if args.reference_dim == args.candidate_dim:
        vector_cosines = [
            _cosine(left, right) for left, right in zip(reference, candidate)
        ]
        if (
            args.min_vector_cosine is not None
            and statistics.median(vector_cosines) < args.min_vector_cosine
        ):
            failures.append("candidate median vector cosine below threshold")
        reference_rankings = _rankings(
            reference_queries,
            reference_documents,
            top_k=args.top_k,
        )
        mixed_query_overlap = _mean_topk_overlap(
            reference_rankings,
            _rankings(
                candidate_queries,
                reference_documents,
                top_k=args.top_k,
            ),
        )
        mixed_document_overlap = _mean_topk_overlap(
            reference_rankings,
            _rankings(
                reference_queries,
                candidate_documents,
                top_k=args.top_k,
            ),
        )
    elif args.min_vector_cosine is not None:
        failures.append("vector cosine requested for incompatible dimensions")

    speedup = (
        reference_run["document_texts_per_second"]
        and candidate_run["document_texts_per_second"]
        / reference_run["document_texts_per_second"]
    )
    if args.min_speedup is not None and (
        speedup is None or speedup < args.min_speedup
    ):
        failures.append("candidate throughput speedup below threshold")
    if (
        args.max_candidate_query_p95 is not None
        and candidate_run["query_p95_seconds"] > args.max_candidate_query_p95
    ):
        failures.append("candidate query p95 above threshold")

    def _timings(run: dict) -> dict:
        return {
            "query_p50_seconds": round(run["query_p50_seconds"], 4),
            "query_p95_seconds": round(run["query_p95_seconds"], 4),
            "document_p50_seconds": round(run["document_p50_seconds"], 4),
            "document_p95_seconds": round(run["document_p95_seconds"], 4),
            "document_texts_per_second": round(
                run["document_texts_per_second"],
                4,
            ),
        }

    result = {
        "method": {
            "warmups": args.warmups,
            "repetitions": args.repetitions,
            "queries": len(QUERIES),
            "documents": len(DOCUMENTS),
        },
        "reference": _timings(reference_run),
        "candidate": _timings(candidate_run),
        "document_throughput_speedup": (
            round(speedup, 3) if speedup is not None else None
        ),
        "reference_dimension": len(reference[0]),
        "candidate_dimension": len(candidate[0]),
        "reference_max_norm_error": reference_norm_error,
        "candidate_max_norm_error": candidate_norm_error,
        "mean_topk_overlap": round(topk_overlap, 4),
        "mixed_candidate_query_topk_overlap": (
            round(mixed_query_overlap, 4)
            if mixed_query_overlap is not None
            else None
        ),
        "mixed_candidate_document_topk_overlap": (
            round(mixed_document_overlap, 4)
            if mixed_document_overlap is not None
            else None
        ),
        "median_vector_cosine": (
            round(statistics.median(vector_cosines), 6)
            if vector_cosines is not None
            else None
        ),
        "minimum_vector_cosine": (
            round(min(vector_cosines), 6)
            if vector_cosines is not None
            else None
        ),
        "failures": failures,
    }
    print(json.dumps(result, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
