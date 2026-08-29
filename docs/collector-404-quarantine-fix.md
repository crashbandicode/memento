# Collector 0.0.54 — unstructured error responses stay retryable

## Incident

During a brief API deploy/recreate window on 2026-08-28, Traefik routed
`/api/ingest/file` to the web app while the API container was unavailable. The
web app returned HTTP 404 with an HTML `<!DOCTYPE html>...` body. The collector
treated that response as an API-owned permanent error and left the queue rows
terminal forever:

- `status='quarantined'`
- `outcome_state='permanent_quarantine'`
- `diagnostic_code='http_404'`
- `terminal_at` set

The affected rows' `last_error` began
`/api/ingest/file returned HTTP 404: <!DOCTYPE html><html lang="en" ...`.
Because a terminal row prevents a new row for the same file, the two affected
primary Claude threads (rows 123737 and 131458) required manual revival.

## Classification change

The shared upload-response classifier now treats an error response as an
API-owned disposition only when it is structured: its `Content-Type` declares
JSON, or its body parses to the API error shape (a JSON object with `detail`).

An unstructured error response — including the observed HTML 404 and the same
405/410 router/proxy hazard — is now `transient_retry` with
`diagnostic_code='unstructured_http_error'`. It reuses the durable queue's
ordinary backoff, matching connection-refused behavior, and therefore retains
the payload for later delivery rather than setting a terminal row.

## Explicit non-changes

- Structured API 404s and other genuine permanent errors retain their existing
  permanent disposition.
- Structured 409 `spool_job_terminal` behavior remains quarantined.
- The guarded-delta 409 `delta_base_mismatch` path remains its existing source
  repair flow.
- Existing terminal rows are not rewritten by this release; operators should
  use the existing requeue/revival procedure where needed.

## Verification

- Focused classifier suite: `32 passed, 169 subtests passed in 1.11s`.
- Full collector suite: `320 passed, 2 skipped, 169 subtests passed in 30.96s`
  (`pytest collector/tests -q --basetemp .pytest-tmp`).

## Rollout

This is a collector/sidecar change. Build and deploy the 0.0.54 sidecar to each
fleet machine using the documented 0.0.53 per-machine rollout procedure; no
server rollout is required for this classifier-only fix.
