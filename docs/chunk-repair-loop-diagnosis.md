# Chunk-Upload Repair Loop — Root-Cause Diagnosis

Investigation date: 2026-08-27 (UTC). Read-only investigation. No code, config, server data,
services, or queue rows were modified. Machine: **butterbridge** (server stack in WSL Docker);
affected collector on **dreamland-yoga** (reached over SSH, read-only).

Repo: `C:\Users\intpa\OneDrive\Documents\test\memento-control-plane` (branch `main` @ 59608248e0).

---

## 1. Problem summary

The Memento collector on **dreamland-yoga** is stuck in a tight upload loop for two giant Codex
rollout JSONL files:

- `sessions/2026/08/13/rollout-2026-08-13T07-38-05-019ffaea-48b9-7321-b599-93ab4db27844.jsonl`
- `sessions/2026/08/07/rollout-2026-08-07T12-22-54-019fdd08-e1a4-7f71-82e7-b0891c2ef9c1.jsonl`

For each file the collector repeatedly uploads a **bounded ~16 MiB delta tail** (9 × 2 MiB chunks)
via `POST /api/ingest/file/chunk`. The server rejects **chunk 0 with HTTP 400**, detail
`"chunk metadata conflicts with existing upload"`. The collector maps 400 → `source_repair_required`
with `available_at=0` and no `repair_action`, parks the row as `repair_required`, and then the
file-scan/enqueue path immediately revives the same row to `pending` and re-sends the identical
payload. Measured live rate: **92 chunk-400s in 10 minutes** (~1 every 6.5 s; currently dominated by
the still-growing Aug-13 file).

**Impact: bandwidth only. No data loss.** Both target documents are already committed in Postgres
(`84a3e717…` Aug-13, `8e88f67d…` Aug-07) and were last updated 2026-08-27 11:38 / 11:30 UTC by a
full-rebase; the looping uploads change nothing.

**Verdict (details in §5): BOTH sides are buggy.** The collector's escalation is the proximate cause
of the infinite, zero-backoff loop; the server contributes a genuine bug — a retained,
terminally-superseded spool job poisons every re-upload and the 400 it emits is non-actionable
(it hides the real disposition, so the collector never rebases and recovers).

There are **two interleaved cycles** against these two files (coordinator question (a)):

- **Cycle A — the HTTP-400 loop (the subject of this report):** bounded 16 MiB **chunked** delta
  with a stale base (~33–50 MiB), `delivery_identity=null` (plain chunk path, not realtime
  admission). Rejected **synchronously** at chunk 0 with 400. This is what wastes bandwidth now and
  creates **no** new server spool jobs (it hits a leftover manifest and raises).
- **Cycle B — accepted-but-superseded realtime deltas:** small (1–32 KiB) single-chunk deltas at the
  file *head* (~210–230 MiB), `admission_identity`/`payload_sha256` set, HTTP-accepted then failed
  during async drain with `RawWriterUnsupported` and blocked `superseded_by_full_rebase`. In the live
  spool these are all from a burst **11:07–11:12 UTC today**; no spool job for either file is newer
  than 11:12, so Cycle B is not currently flooding the spool (see §3.3 — this refines the orchestrator
  tip, which I was asked to verify rather than assume).

---

## 2. Server side — the 400 raise site and live evidence

### 2.1 Raise path

`POST /api/ingest/file/chunk` → `server/server/api/ingest.py:1102` `ingest_file_chunk()`. It reads
chunk 0 and calls `stage_chunk(...)`; the only 400 from the staging body is:

```
ingest.py:1152   staged = await asyncio.to_thread(stage_chunk, meta=meta, chunk_data=chunk_data, ...)
ingest.py:1163   except ChunkValidationError as exc:
ingest.py:1164       raise HTTPException(status_code=400, detail=str(exc)) from exc
```

`stage_chunk` lives in `server/server/services/ingest_spool.py:598`. The relevant flow on a
re-upload:

- The durable **job identity** is `sha256([user_id, device_id, upload_id, hash])`
  (`ingest_spool.py:88-105`). When there is no `delivery_identity` (the plain chunk path used here),
  it is keyed on `upload_id` + `hash` only — i.e. effectively on **(user, device, tool, relative_path,
  content_hash)**. It does **not** include `base_hash`, `base_offset`, `file_size`, `total_chunks`,
  or `timestamp`.
- On chunk 0, if a `manifest.json` already exists for that job id, `stage_chunk` compares the incoming
  metadata against the stored manifest:
  - immutable fields (`job_id,user_id,device_id,device_name,device_platform,total_chunks`) →
    raise `"chunk metadata conflicts with existing upload"` at `ingest_spool.py:670`;
  - meta fields (`upload_id,hash,tool,relative_path,category,content_type,mode,offset,file_size,
    sync_strategy,metadata,timestamp,base_hash,base_offset,authoritative_rebase,delivery_identity,
    admission_identity,payload_sha256,total_chunks`) → raise the **same** string at
    `ingest_spool.py:696`. Note **`timestamp` is one of the compared fields (`:686`).**

Crucially, `stage_chunk` checks for a *completion receipt* (`completion_path`, `:622-627`) and for an
admission-payload conflict (`:618`), but it does **not** consult `failed.json` / `blocked.json`. So a
job that has already **terminally failed and been blocked** still owns a live `manifest.json`, and any
re-upload whose metadata differs by even one field (here: `timestamp`) is rejected with a generic 400.

### 2.2 Which validation actually fires (ground truth)

The collector persists the server's 400 body verbatim. Both looping queue rows on yoga hold:

```
last_error = /api/ingest/file/chunk returned HTTP 400: {"detail":"chunk metadata conflicts with existing upload"}
http_status = 400, outcome_state = source_repair_required, diagnostic_code = http_400
```

So the raise site is the **manifest-immutability comparison** (`ingest_spool.py:696`, the meta-field
loop), not an input-shape validation. Confirmed differing field = **`timestamp`** (proof in §3.2/§4).

### 2.3 What the pre-existing manifest is, and why it is terminal

The current looping payloads map byte-for-byte to two retained spool jobs (found by grepping
`/data/ingest-spool/*/manifest.json`):

| File | Spool job id | manifest `hash` (content_hash) | offset / file_size / base_offset | chunks | delivery_id | failed.json | blocked.json |
|------|--------------|-------------------------------|----------------------------------|--------|-------------|-------------|--------------|
| Aug-13 | `03364935…` | `d2:6edce19…` | 50341747 / 16778332 / 33559692 | 9 | null | `DeltaBaseMismatch` (attempts 1) | `superseded_by_full_rebase` |
| Aug-07 | `f6459dd6…` | `d2:a5dcd7b4…` | 67157450 / 16777699 / 50376396 | 9 | null | `DeltaBaseMismatch` (attempts 1) | `superseded_by_full_rebase` |

Both job dirs contain `manifest.json` + `chunk-000000..8.bin` + `ready` + `failed.json` +
`blocked.json` (+ `payload.bin`, `sanitized.bin`). `blocked.json` records the superseding full-rebase
job and the document id:

```
{"reason":"superseded_by_full_rebase","superseding_job_id":"2e912ce5…","document_id":"84a3e717-e05b-4604-b9f5-82688f0d1bf0", ...}   # Aug-13
{"reason":"superseded_by_full_rebase","superseding_job_id":"4f9edcee…","document_id":"8e88f67d-b24f-4ea6-ae8b-3a846f3cec95", ...}   # Aug-07
```

The disposition trail (coordinator question (b)):

- The bounded tail's declared base (`base_offset≈33.5 MiB / 50 MiB`, `base_hash d2:7f23…` / `d2:9973…`)
  no longer matches the committed document, which had already been **full-rebased** far past that
  point. The drain therefore raised **`DeltaBaseMismatch`**, which is in the "permanent" set at
  `server/server/tasks/ingest_spool.py:704-716` (`:709`) → `mark_job_failed(...)` (`:728`, writes
  `failed.json`).
- A later full-rebase for the same source then **superseded** the delta job. Because the candidate is
  a delta (or already has `failed.json`), it is **retained as evidence** and only *blocked*, never
  removed: `tasks/ingest_spool.py:678-690` (`retain_as_evidence` → `mark_job_blocked` `:685`).
  `complete_and_remove_job` (`:691-698`) only removes non-evidence/full jobs. **Nothing ever deletes
  the retained delta job dir**, and the 24 h stale-incomplete sweep does not apply (the job is
  `ready`, not incomplete). The manifest therefore poisons re-uploads indefinitely.

(Cycle B's small realtime deltas failed differently — `RawWriterUnsupported`, `tasks/ingest_spool.py:208`
— which has a legacy-writer fallback `:208-244`; they were likewise blocked `superseded_by_full_rebase`.)

### 2.4 Live server evidence

`wsl docker logs memento_api` shows the loop continuously:

```
INFO: 172.18.0.4:… - "POST /api/ingest/file/chunk HTTP/1.1" 400 Bad Request
INFO: 172.18.0.4:… - "POST /api/ingest/file/chunk/status HTTP/1.1" 200 OK   (other, working uploads)
…
chunk_400_in_last_10min = 92
```

Postgres corroboration (both target docs committed, unchanged since the morning rebase; no data loss):

```
84a3e717-e05b-4604-b9f5-82688f0d1bf0 | created 2026-08-13 | updated 2026-08-27 11:38:33+00
8e88f67d-b24f-4ea6-ae8b-3a846f3cec95 | created 2026-08-07 | updated 2026-08-27 11:30:40+00
```

No new spool job for either file exists after 11:12 UTC (newest spool dirs at query time were 20:09–20:13
for *other* files). This proves the 400 loop hits the leftover manifest and raises **without** creating
spool work — pure HTTP bandwidth waste.

---

## 3. Collector side — queue evidence from yoga (read-only)

Queue DB: `C:\Users\intpa\.memento\sync_queue.db` (195 MB, live WAL; default
`config.py:183 queue_db_path = _default_data_dir()/"sync_queue.db"`, `_default_data_dir = HOME/.memento`).
No `sqlite3` CLI on yoga; copied the db+wal+shm to `%TEMP%\sqcopy` and queried the copy with
`C:\Python314\python.exe` (read-only). `config.json` was **not** printed (may contain the server token).

### 3.1 Status / outcome distribution

```
STATUS_COUNTS:   synced 293 | quarantined 24 | repair_required 2 | superseded 2 | accepted 1 | pending 1 | uploading 1
OUTCOME_COUNTS:  success 293 | permanent_quarantine/http_404 24 | source_repair_required/http_400 2 | accepted 2
```

Exactly **2** rows are `source_repair_required / http_400` — the two rollout files.

### 3.2 The two looping rows

```
id 227530  codex  sessions/2026/08/13/rollout-…019ffaea….jsonl
  sync_strategy=delta  is_partial=1  offset=50341747  file_size=16778332  payload_bytes=16778332
  base_hash=d2:7f23759504f28…  base_offset=33559692  content_hash=d2:6edce19…
  status=repair_required  outcome_state=source_repair_required  diagnostic_code=http_400  http_status=400
  retry_count=1  available_at=0.0  source_modified_at=2026-08-27T20:02:20Z
  last_error="/api/ingest/file/chunk returned HTTP 400: {\"detail\":\"chunk metadata conflicts with existing upload\"}"
  (live file on disk: 255,076,725 bytes, mtime 2026-08-27T20:14:23Z — still growing)

id 227525  codex  sessions/2026/08/07/rollout-…019fdd08….jsonl
  delta  is_partial=1  offset=67157450  file_size=16777699  base_hash=d2:9973c703…  base_offset=50376396
  content_hash=d2:a5dcd7b4…  status=repair_required  http_400  retry_count=1  available_at=0.0
  source_modified_at=2026-08-27T13:10:22Z   (live file: 213,685,000 bytes, mtime 13:10:22Z — stopped growing)
```

`content_hash` values match the retained manifests' `hash` (`03364935`/`f6459dd6`) → the re-upload
resolves to the **same job id** → hits the poisoned manifest.

**The conflicting field is `timestamp`:** the payload `timestamp` = `source_modified_at` (row
`ingest.py:598-602` sets `timestamp = item.source_modified_at`). Row 227530 sends
`1787860940` (20:02:20Z) but the frozen manifest `03364935` has `timestamp 1787828988` (11:09:48Z);
row 227525 sends `1787836222` (13:10:22Z) vs manifest `f6459dd6` `1787829021` (11:10:21Z). Every other
compared field matches. Because the Aug-13 file is still being appended (mtime advances), its
`source_modified_at` keeps increasing and can **never** equal the frozen manifest timestamp → perpetual
conflict. (Aug-07's file stopped at 13:10, so it sends a now-stable 13:10 timestamp that still ≠ 11:10 —
also perpetual, but only re-triggered when a new file event re-enqueues it.)

### 3.3 Why the base is stuck (file_state cursor)

```
codex Aug-13: synced_offset=33559692 (synced_at 11:13:31Z)  observed_offset=50341747 (observed_at 20:02:22Z)
codex Aug-07: synced_offset=50376396 (synced_at 11:12:57Z)  observed_offset=67157450 (observed_at 13:10:26Z)
```

The delta cursor's `synced_offset` equals the looping delta's `base_offset` and has been **frozen since
~11:13** (right before the server-side full-rebase completed ~11:30–11:38). The bounded repair keeps
emitting `[synced_offset, synced_offset+16 MiB)`; it can never commit (base no longer exists on the
server), so `synced_offset` never advances and the same window is re-sent forever. Note this delta
cursor (≤67 MiB) is separate from Cycle B's realtime/admission path, which reached ~230 MiB this
morning — hence two independent cursors over the same files.

---

## 4. Escalation logic — why no backoff, why every few seconds

### 4.1 Status → outcome mapping (no repair_action)

`collector/collector/sync_client.py:38`:
```
SOURCE_REPAIR_HTTP_STATUSES = frozenset({400, 413, 422})
```
`_classify_http_response` (`:63-102`) maps any 400/413/422 to:
```
sync_client.py:92  if status in SOURCE_REPAIR_HTTP_STATUSES:
sync_client.py:93      return UploadOutcome.source_repair(diagnostic, diagnostic_code=f"http_{status}", http_status=status)
```
This `UploadOutcome` has **`repair_action=None`** and `expected_offset=0` (see `outcomes.py:95-114`).

In the worker (`sync_client.py:407-444`), `source_repair_required` is only *actionable* when it carries
a `repair_action`:
- `DELTA_BASE_CONFLICT` (`:407-417`) → `mark_delta_conflict` + adopt server base + schedule repair;
- `REBUILD_BOUNDED_DELTA` (`:418-430`) → `mark_repair_scheduled` + full resync.
- **else (repair_action None) → `:431-444` `mark_upload_outcome(...)`** with no corrective action.

`mark_upload_outcome` → `record_outcome` (`collector/collector/queue.py`):
```
queue.py:2486  elif outcome.state is UploadOutcomeState.SOURCE_REPAIR_REQUIRED:
queue.py:2487      status = "repair_required"
queue.py:2488      available_at = 0.0
```
So the row is parked terminal with **no backoff** and **no attempt cap**.

### 4.2 In-place revival wipes the terminal state every scan

The row does not re-lease itself (leases only pick `status='pending'`). The loop is driven by the
file-scan/enqueue path. For a coalescible delta, `enqueue` looks for an existing row with the **same
`base_hash` + `base_offset`**, explicitly including terminal `repair_required`/`quarantined`:
```
queue.py:1753  elif is_coalescible_delta:
queue.py:1754      existing = … WHERE … status IN ('pending','auth_blocked','repair_required','quarantined')
queue.py:1761          AND sync_strategy='delta' AND is_partial=1 AND base_hash=? AND base_offset=?
```
and **updates it back to live**, resetting the terminal markers:
```
queue.py:1769-1808  UPDATE queue SET … retry_count=0, status='pending', available_at=0,
                     last_attempt_at=NULL, terminal_at=NULL … WHERE id=? AND status IN (…,'repair_required',…)
```
Because `synced_offset` (=`base_offset`) is frozen, every re-observation of the growing file produces a
delta with the *identical* base, so it matches and revives the same row (id 227530/227525) — which is
why `retry_count` stays at 1 (reset each revive) instead of climbing, and why only 2 rows ever exist.
The cadence (~3–6 s) is the file-change/scan interval on the actively-growing Aug-13 file
(`config.py:189-208`: debounce 0.3 s, sync_interval 0.5 s), not a retry timer. `prioritize_file`
(`queue.py:3084-3106`, `created_at=0, available_at=0`) can additionally push such a repair ahead of the
backlog.

### 4.3 Intended semantics vs. what happens

`source_repair_required` was designed to be paired with a `SourceRepairAction`
(`outcomes.py:23-27`) that **changes the next payload**: adopt the server's committed base
(`DELTA_BASE_CONFLICT`) or rebuild from a full snapshot (`REBUILD_BOUNDED_DELTA`). A **generic
400/413/422 with `repair_action=None` carries no corrective action**, so the collector re-sends the
*identical, doomed* bytes. Combined with `available_at=0` and the in-place revival (which erases any
terminal state and any would-be backoff), a single legitimate rejection becomes an unbounded, tight,
zero-backoff retry storm. There is no escalation to `quarantine` after N failed repair attempts.

---

## 5. Verdict — server bug vs collector-escalation bug vs both

**Both — and they compound.** Root cause, in order:

**Primary (collector escalation).** A 400 mapped to `source_repair_required` **without a
`repair_action`** is retried forever with **zero backoff and no attempt cap**, and the enqueue path
**revives the terminal `repair_required` row in place** (`queue.py:1753-1808`), wiping the terminal
state on every file event. Even a perfectly correct 400 must not be re-sent identically ad infinitum.
This is the direct cause of the bandwidth-wasting loop and it would occur for *any* non-actionable 4xx.
Evidence: `sync_client.py:92-97`, `queue.py:2486-2488`, `queue.py:1769-1808`; yoga rows 227530/227525
(`available_at=0`, `retry_count=1`, ~92 400s/10 min).

**Contributing (server).** The 400 is genuinely *wrong* as a disposition:
1. A **terminally failed + superseded delta job is never cleaned up** — it is retained "as evidence"
   (`tasks/ingest_spool.py:678-690`) but keeps a live `manifest.json`, and `stage_chunk` does not check
   `failed.json`/`blocked.json`. So the leftover manifest **poisons every re-upload** of the same job id.
2. `stage_chunk`'s manifest-immutability check treats a legitimately-evolving `timestamp` (source mtime
   of a growing file) as a conflict (`ingest_spool.py:686,696`), even though `timestamp` is not part of
   the job identity. Result: a **non-actionable generic 400** instead of the real disposition.
3. The real disposition — the delta base is behind a full-rebased document (`DeltaBaseMismatch`) — is
   detected **only asynchronously** in the drain and is **never surfaced to the collector** on the chunk
   path. The server *has* a base-conflict channel (`ingest.py:327 _delta_mismatch_response` → 409 with
   expected hash/offset, which the collector handles as `DELTA_BASE_CONFLICT` → rebase), but the chunk
   endpoint never uses it. So the collector is denied the one signal that would let it rebase and
   actually sync, and instead loops on a doomed stale-base tail.

Net: the collector turns a rejection into an infinite loop; the server both causes the rejection
(stale-manifest poison) and makes it unrecoverable (no base-conflict signal). Fixing only one side
stops the *symptom* (bandwidth) but not the underlying inability to sync these files.

---

## 6. Recommended fixes (root-cause first) — NOT IMPLEMENTED

### Root-cause fixes

**R1 (server) — Do not let a terminal job poison re-uploads; return an actionable disposition.**
In `stage_chunk`, before the manifest-immutability comparison (`ingest_spool.py:652-698`), detect a
retained-terminal job (`failed.json` and/or `blocked.json` present) and respond with a *stable,
actionable* result instead of a generic 400: e.g. a **409 delta-base-conflict** carrying the committed
head hash/offset (reuse `_delta_mismatch_response`, `ingest.py:327`) when the failure was
`DeltaBaseMismatch`/`superseded_by_full_rebase`, or a terminal "already superseded" signal. This makes
the collector adopt the server base (`DELTA_BASE_CONFLICT` path) and stop resending.

**R2 (server) — Reclaim superseded delta job dirs.** Blocked/`superseded_by_full_rebase` delta jobs are
retained forever (`tasks/ingest_spool.py:678-690`) with no reaper. Add bounded GC (age/space-based, like
the existing stale-incomplete sweep) so a superseded manifest cannot poison future uploads. Consider
also excluding `timestamp` from the manifest-immutability comparison (`ingest_spool.py:686`) since it is
not part of the job identity and legitimately changes for an append-only file.

**R3 (collector) — Make a 4xx repair actually change the payload, and cap it.** A
`source_repair_required` with `repair_action=None` must not re-send identical bytes. On repeated 400 for
the same `(base_hash, base_offset)` the collector should either request/adopt the server's committed base
(rebase) or trigger `REBUILD_BOUNDED_DELTA` (full resync), and **escalate to `quarantine` after a small
attempt cap** so a permanently-rejected revision cannot loop. Also stop the in-place revival
(`queue.py:1769-1808`) from resetting `retry_count`/`terminal_at` for `repair_required` rows, so failed
repairs accumulate toward that cap instead of resetting every scan.

### Fast band-aid (clearly a band-aid — does not fix the sync failure)

**B1 — Add backoff to `source_repair_required`.** In `record_outcome`
(`queue.py:2486-2488`), set `available_at = now + _retry_delay_seconds(retry_count)` (capped
exponential, as already used for `TRANSIENT_RETRY` at `queue.py:2482`) instead of `0.0`, and have the
enqueue-revival path (`queue.py:1769-1808`) **preserve** `available_at`/`retry_count` for
`repair_required` rows rather than zeroing them. This throttles the 400 storm from ~1/6 s to minutes
while R1–R3 are developed. It does **not** let the two files sync — they remain stuck until the base
conflict is resolved (R1/R3) or the files roll over.

---

## Appendix — commands / access notes

- Server logs/spool/db: `wsl docker logs memento_api`, `wsl docker exec memento_api …`,
  `wsl docker exec memento_postgres psql -U postgres -d memento …` (butterbridge PowerShell 7).
- Yoga (read-only): `ssh intpa@dreamland-yoga.local` (cmd.exe) → MSI
  `C:\Program Files\PowerShell\7\pwsh.exe -EncodedCommand …`; queue DB copied to `%TEMP%\sqcopy` and
  read with `C:\Python314\python.exe`. No process/task/service/desktop-app on yoga was touched.
- No tokens/JWTs were printed or persisted; `config.json` on yoga was intentionally not dumped.
