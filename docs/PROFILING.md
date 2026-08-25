# On-demand profiling

Memento ships two opt-in profilers. Both are inert until you turn them on, so
there is no steady overhead in normal operation.

- **Request profiler (pyinstrument)** — profile a single HTTP request end to
  end and get an HTML flame graph back. Best for API hotspots and slow
  endpoints.
- **py-spy** — attach to a *running* process (the API or a Celery ingest
  worker) with no redeploy and no code change. Best for background/ingest work
  and for grabbing a stack when something is pegged right now.

## 1. Request profiler (pyinstrument)

### Enable it

Off by default. An operator sets both, then restarts (or recreates) the API:

```
MEMENTO_PROFILING_ENABLED=1
MEMENTO_PROFILING_TOKEN=<a long random secret>
```

Both are required. With the flag off (production default) the middleware is a
single boolean check — it never imports pyinstrument or starts a timer.

### Profile a request

Add `?profile=1` and present the token, on any authenticated request you want
to inspect:

```
curl -H "Authorization: Bearer $JWT" \
     -H "X-Memento-Profile-Token: $MEMENTO_PROFILING_TOKEN" \
     "https://memento.example.com/api/conversations/<id>/prompts?profile=1" \
     -o profile.html
```

The response body is replaced with a pyinstrument flame graph (open
`profile.html` in a browser). The graph includes time spent across `await`
points (async mode), so DB round-trips and downstream calls show up.

- The token may also go in the query string (`&profile_token=...`) if headers
  are inconvenient.
- A request that asks to profile but presents a wrong/missing token is served
  as a **normal request** — no flame graph, no signal that profiling exists.
- The graph contains code paths and timings only — never request bodies or
  credentials.

### Turn it off

Unset `MEMENTO_PROFILING_ENABLED` (and rotate the token). Leaving it enabled is
low-risk given the secret gate, but disabling it removes the surface entirely.

## 2. py-spy (attach to a live process)

`py-spy` is baked into the server image. It profiles a process from the
outside — no restart, no instrumentation — which is the right tool for the
Celery ingest workers and for "it's hot *now*, what's it doing?".

Attaching to another process needs `SYS_PTRACE`. Either add it to the service:

```yaml
  celery-ingest-worker:
    cap_add:
      - SYS_PTRACE
```

…or run the `exec` as root (`docker exec -u 0 ...`).

### One-shot stack dump (what is it doing right now)

```
docker exec memento_celery_ingest_worker py-spy dump --pid 1
```

### Flame graph over a window

```
docker exec memento_celery_ingest_worker \
  py-spy record -o /tmp/ingest.svg --pid 1 --duration 30 --subprocesses
docker cp memento_celery_ingest_worker:/tmp/ingest.svg ./ingest.svg
```

`--subprocesses` follows Celery's worker children. Use `--pid 1` for the API
container too (uvicorn is PID 1 there).

### Live top-style view

```
docker exec -it memento_celery_ingest_worker py-spy top --pid 1
```

## When to reach for which

| Symptom | Tool |
| --- | --- |
| One endpoint is slow / high p95 | Request profiler (`?profile=1`) |
| Ingest/sync is CPU-heavy or a worker is pegged | `py-spy record` on the worker |
| "What is PID 1 stuck on right now?" | `py-spy dump` |
| Steady, cross-request trends (latency, queries/req, RSS) | Not profiling — add metrics (see below) |

## Note: metrics vs profiling

Profiling answers "why is *this* slow." For "is anything trending wrong over
time" (request-latency histograms, Celery task durations, DB-queries-per-request
to catch read-amplification, per-worker RSS trend), add lightweight always-on
metrics instead — that is cheap and catches regressions before they need a
profiler. That layer is not included here; it is the recommended companion.
