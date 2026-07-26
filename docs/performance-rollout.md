# Performance rollout handoff

## Active candidate

- Branch: `feat/postgres-embedding-efficiency`
- Database volume: `memento_pgdata_performance`
- Original rollback volume: `memento_pgdata`
- Original size at clone: `16,758,955,304` bytes
- Candidate size after clone: `16,758,955,304` bytes
- Clone method: stopped API/Celery/PostgreSQL writers, then copied the stopped
  volume byte-for-byte with metadata preserved.
- The original volume has not been mounted by the candidate PostgreSQL
  container and must not be deleted.
- Docker Compose on this branch defaults `pgdata` to the candidate volume.
  The original branch's unchanged Compose file defaults to `memento_pgdata`.

## Deployed safety defaults

- PostgreSQL: 256 MB `shared_buffers`, 1 GB `effective_cache_size`, SSD planner
  costs, one parallel gather worker, spread checkpoints, larger WAL, and more
  frequent/autovacuum-friendly table settings.
- Quality embeddings: torch/BGE-M3, 1024 dimensions, 0.75 CPU, one native
  inference thread.
- Tiering: disabled until the fast model passes ranking/multilingual checks.
  Existing fast rows remain searchable if the flag is later disabled.
- The fast-tier candidate default is multilingual E5-small with explicit
  `query: ` / `passage: ` purposes. Tiering remains disabled: the current
  character chunker can still truncate 512-token models, and the measured
  retrieval gap has not passed the rollout gate.
- ONNX int8: disabled. An isolated, checksummed BGE-M3 candidate was 2.3-3.1x
  faster at the test quota, but missed the vector and ranking gates. A local
  per-channel conversion also exceeded a hard 5 GiB memory limit; Docker
  recorded the OOM without affecting production.
- Embedding profile identity now fingerprints model revision, backend,
  dimensions, prefixes, sequence-length policy, ONNX filename/checksum,
  pooling policy, and normalization. Search excludes incompatible rows.
- ONNX startup verifies the installed/actual provider, constrains ORT session
  threads, checks the artifact checksum when locally available, and probes
  output count/dimensions/finiteness/unit norm before reporting ready.
- Live conversation finalization defaults to a ten-minute quiet window. Exact
  unchanged chunks are still reused; appends do not force a full vector rewrite.
- Optional search database: disabled unless
  `MEMENTO_SEARCH_DATABASE_URL` is explicitly configured.

## Validation

- Docker Compose configuration validated with the embedding profile.
- Focused embedding/routing regressions: 71 passed, 33 subtests passed.
- Full server suite: 485 passed, 71 subtests passed, 3 existing deprecation
  warnings.
- Candidate PostgreSQL is mounted on `memento_pgdata_performance`.
- API, PostgreSQL, Redis, embedding, web, and all Celery services started.
- Embedding health reports BGE-M3 / torch / CPU / 1024 dimensions.
- The API and Celery services were deployed with the ten-minute conversation
  quiet window and profile-signature schema migration without recreating the
  embedding container. Both vector tables now have `profile_signature`; the
  existing BGE-M3 container remained healthy and kept its 22-hour uptime.
- Direct-DB MCP now mounts successfully at `/mcp` and uses the configured
  read-only search session plus fast-tier search support.
- Daily backup now covers both vector tables, spills past 64 MB to disk, uses
  low-CPU gzip level 1, and streams to MinIO. The pre-fix 4.48 GB compressed
  backup completed successfully before the updated worker was deployed.
- On 2026-07-22, the initial retry backlog was drained with a temporary,
  untracked Compose override. Quality inference was raised first to 2 CPUs /
  2 threads and then to 4 CPUs / 4 threads. Observed 50-chunk jobs fell from
  about 527 seconds at the 0.75-CPU cap to roughly 154-208 seconds at 4 CPUs;
  the container consumed 378-395% CPU while active.
- Ten static documents moved to `ok`. The only two recoverable documents left
  at handoff were live Cursor/Codex transcripts still inside the intentional
  three-minute quiet window. One unrelated legacy LSF document remains in a
  two-day-old `processing` state at the five-attempt ceiling and is not active.
- The temporary override was deleted and the embedding service was recreated
  at the deployed 0.75-CPU / one-thread default. Across six restored-state
  samples, embedding was normally 0.01-0.06% CPU (one 2.87% health/housekeeping
  spike), PostgreSQL settled near 0.16-1.32% after two 8.66-17.40% bursts, the
  API settled near 0.20-1.01%, and both Celery workers stayed below 0.4%.
- Later the same evening Butterbridge felt hot again. That was not a regression
  of the quiet caps: OneDrive was burning ~128% CPU, and Memento was doing
  expected live-transcript work under the 0.75-CPU ceiling (one 50-chunk Codex
  session took ~695s). After OneDrive was killed/disabled, embedding returned
  to ~0.01% CPU with backlog mostly `ok`. The stale legacy LSF claim at the
  five-attempt ceiling was marked `failed` so status stays truthful.

## Fast-tier benchmark (2026-07-23)

Bounded read-only evaluation used 16 production conversation samples plus ten
multilingual/code cases on the i7-11370H. Candidate services were limited to
0.5 CPU/one thread; BGE-M3 used 0.75 CPU/one thread.

- BGE-M3: 0.141 passages/s, 467/505 ms query p50/p95, 2.51 GiB peak RSS,
  Recall@1/5 0.615/0.769, MRR@10 0.670, cross-lingual Recall@5 1.00.
- BGE-small-en-v1.5: 2.015 passages/s (14.3x), 84/164 ms query p50/p95,
  681 MiB peak RSS, Recall@1/5 0.577/0.615, MRR@10 0.601. Its worst
  cross-lingual target ranked 23rd.
- Multilingual E5-small: 1.679 passages/s (11.9x), 84/88 ms query p50/p95,
  1.04 GiB peak RSS, Recall@1/5 0.539/0.692, MRR@10 0.595. Its worst
  cross-lingual target ranked 9th.
- Both small models truncated 61.5% of the current 2,000-character chunks.

Decision: retain BGE-M3 and keep tiering off. E5-small is the safer next
candidate only after tokenizer-aware chunking, purpose prefixes, shadow
backfill, and a labeled multilingual canary meet the gates below.

## ONNX/int8 proof (2026-07-23)

A bounded one-CPU/two-GiB MiniLM proof validated the runtime path without
touching the live BGE-M3 service or production volumes.

- FP32 ONNX cosine versus torch was effectively 1.0.
- AVX512-VNNI dynamic int8 median/min cosine was 0.99326/0.98942.
- Tuned VNNI (ORT intra/inter-op threads = 1) reached 316.4 texts/s versus
  torch's 159.5 texts/s. Untuned VNNI regressed to 95.6 texts/s, confirming
  that explicit ORT thread control is required.
- The VNNI artifact was 23.0 MB versus 90.4 MB FP32 ONNX.

The follow-up used an isolated lab with no production volumes. The exact live
torch image was retained as
`memento-embedding:torch-snapshot-20260723-1528`
(`sha256:020021fc0ace...`). Candidate/reference containers were limited to
0.5 CPU and one inference thread and were run sequentially.

- The pinned official BGE-M3 FP32 graph and external weights matched their
  published SHA-256 values (`f8425123...` and `1eebfb28...`). FP32 ONNX matched
  torch with median/min cosine `0.999999999998/0.999999999983` and identical
  top-1/top-5 rankings.
- FP32 ONNX used 1.73 GiB RSS. It improved query throughput only from 2.56 to
  2.92 texts/s and reduced document throughput from 1.60 to 1.52 texts/s, so
  ONNX alone does not justify a production migration.
- The pinned 569,958,496-byte dynamic-int8 artifact matched SHA-256
  `16de7ea1146ca427e14938ec3e9abfdcaff0e6ac76434cd693ac35d761250bcb`.
  It reached 7.85 query texts/s and 3.71 document texts/s, about 3.1x and 2.3x
  the capped torch reference.
- Int8 short-input cosine missed the gate: query median/min was
  `0.98329/0.97833`, document median/min was `0.98103/0.97518`, and one of
  eight top-1 results changed. Across three 603-4,053-token inputs, median/min
  cosine fell to `0.92116/0.75917`.
- Int8 RSS was 1.18 GiB after short batches but retained 3.83 GiB after the
  4,053-token case, which took 123.6 seconds at the half-CPU cap.
- A local dense-output, per-channel MatMul quantization of the verified FP32
  graph was attempted in a disposable container. Copying the external data
  resolved ONNX's symlink rejection, after which the quantizer exceeded its
  hard 5 GiB memory limit. Docker recorded `oom` and exit 137 after 43 seconds;
  production stayed healthy and no partial candidate was retained.

Decision: FP32 proves the BGE-M3 ONNX path is correct, while the tested dynamic
int8 artifact fails quality and long-input gates. Keep both ONNX modes disabled.
Any next quantization attempt should use a dedicated 32-GiB-or-larger builder
and evaluate selective/per-channel or calibrated static quantization against a
larger labeled multilingual retrieval set.

## Enablement gates

Fast tier:

- At least 100 labeled English, Chinese, cross-language, code, and error queries.
- Per-language Recall@20 at least 95% of BGE-M3; MRR/nDCG loss at most 5%.
- Zero tokenizer truncation after profile-aware chunking.
- Warm query p95 at most 150 ms at 0.5 CPU; peak RSS at most 1.25 GiB.
- Resumable shadow backfill and one-step rollback to retained quality vectors.

BGE-M3 ONNX int8:

- FP32 ONNX median/min cosine at least 0.9999/0.999 versus torch.
- Int8 median cosine at least 0.995 and first percentile at least 0.98.
- Recall/NDCG@10 loss at most one percentage point; top-10 overlap at least 0.95.
- At least 1.25x sustained throughput with no p95 regression under the exact
  0.75-CPU/one-thread quota.
- No thread oversubscription, OOM, swap growth, unresolved external-data
  symlinks, checksum mismatch, or health starvation during a 30-minute soak.
- Blue/green sidecar rollback; never overwrite the current torch image or
  production vector rows during evaluation.

## Rollback

1. Stop the candidate stack without removing volumes:
   `docker compose --profile embedding stop`
2. Switch to the pre-candidate branch/revision.
3. Start that revision normally:
   `docker compose --profile embedding up -d --build`
4. Verify PostgreSQL mounts `memento_pgdata`, then verify `/health`.

Do not run `docker compose down -v`, `docker volume prune`, or delete either
PostgreSQL volume during evaluation. Rollback abandons the candidate's newer
writes; export any data created after cutover first if it must be retained.

## Follow-up

- Add tokenizer-aware chunking and shadow/canary states before any fast-tier
  backfill.
- Build selective/per-channel and calibrated-static BGE-M3 int8 artifacts on a
  dedicated 32-GiB-or-larger builder, then run the labeled retrieval gates.
- Observe steady-state PostgreSQL, Celery, and embedding CPU after the scheduled
  daily backup and initial retry queue have drained.
