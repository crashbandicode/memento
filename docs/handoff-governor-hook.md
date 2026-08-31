# Claude Code handoff governor hook

## Purpose

Long autonomous Claude Code orchestrator sessions need a durable handoff before
their prompt prefix consumes the context window. The governor is a managed,
inert-by-default pair of Claude Code hooks that provides a staged nudge and a
last-resort Stop guard.

## Hook execution and latency

On Windows collector 0.0.56, managed hook registrations invoke the
dedicated `memento-hook-runner.exe`, not the large onefile collector sidecar.
It is a small PyInstaller **onedir** bundle, copied on collector startup from
the desktop application's packaged resource to a versioned directory such as
`%LOCALAPPDATA%\Memento\hooks\0.0.56\`. Its `_internal` directory stays beside
the executable, so a hook process does not unpack a 40 MB onefile bundle into a
new `_MEI*` temp directory for every Claude event.

The installer writes each managed command hook with `"timeout": 10`. This is a
fail-open ceiling if a damaged runner, filesystem problem, or an unexpectedly
slow invocation occurs; normal hook work must complete well below that limit.
The collector installs a new immutable version directory before it rewrites
managed commands. Only after reconciliation points commands at the new runner
does retirement maintenance run. It never renames a version directory. Each
now-unregistered version receives a durable `retired-at.json` marker with an
ISO timestamp and version; a markerless old directory is marked and retained,
never deleted in the same pass. Only collector-daemon startup—not the hook
installer command—may sweep a marker older than 24 hours (tunable with
`MEMENTO_HOOK_RUNNER_RETENTION_HOURS`). The current/registered version and
legacy-sidecar fallback state are never marked or swept.

For an aged, unregistered version, deletion is executable-gated: the first
filesystem mutation is removal of `memento-hook-runner.exe`. Windows refuses
to remove a live process image, so any failure leaves the entire directory
unchanged for a later daemon start. Only after that executable removal succeeds
may the remaining directory be removed; a residual cleanup failure is harmless
because the executable is already gone and the retired version can never launch
again. The daemon/`run` sidecar remains the existing onefile artifact.

On Windows, the packaged source is discovered first at Tauri's resource layout
next to the frozen sidecar:
`<sidecar-dir>\binaries\memento-hook-runner\`. A manual fleet sidecar under
`%LOCALAPPDATA%\Memento\memento-collector-sidecar.exe` uses the analogous
`%LOCALAPPDATA%\Memento\binaries\memento-hook-runner\` location. Direct-build
and non-Windows resource fallbacks remain supported. If no complete runner
source can be found or copied, reconciliation still completes: managed entries
temporarily retain the valid legacy onefile-sidecar command with `timeout: 10`.
That preserves fail-open behavior and, crucially, still removes governor
entries when `MEMENTO_GOVERNOR_ENABLED` is disabled. A later collector start
migrates the commands to the runner once its source is available.

## Load-bearing design decisions

1. **Measure the live prompt prefix, not a display estimate.** The hook reads a
   bounded tail of the transcript named by Claude's stdin hook JSON, finds the
   latest `type="assistant"` record whose `isSidechain` is not `true`, and sums
   `message.usage.cache_read_input_tokens`,
   `message.usage.cache_creation_input_tokens`, and
   `message.usage.input_tokens`. It never uses `cache_read_input_tokens` alone:
   a cold turn can legitimately report zero reads.
2. **Nudges latch once per session and threshold.** The `PostToolUse` `*` hook
   records delivered thresholds in `~/.memento/governor/<session_id>.json`.
   This prevents an over-threshold session from receiving the same context on
   every tool call.
3. **Stop blocks only when the handoff is not real.** At the block threshold,
   the `Stop` hook returns `decision: "block"` only if the configured handoff
   document has no Markdown section mentioning the current `session_id`. It
   never blocks when stdin has `stop_hook_active: true`, preventing a Stop-hook
   loop. There is deliberately no `PreToolUse` denial gate.
4. **Activation is explicit.** The governor is off by default. The collector
   adds its managed `PostToolUse` and `Stop` entries only when enabled, and the
   generated command carries `--enabled`; a fleet rollout alone therefore
   cannot activate the governor.

## Configuration

| Variable | Default | Meaning |
| --- | ---: | --- |
| `MEMENTO_GOVERNOR_ENABLED` | off | Enables managed governor-hook registration when the collector reconciles Claude settings. Truthy values: `1`, `true`, `yes`, `on`. |
| `MEMENTO_GOVERNOR_HYGIENE_TOKENS` | `200000` | First `PostToolUse` context-hygiene nudge. |
| `MEMENTO_GOVERNOR_HANDOFF_TOKENS` | `350000` | Second `PostToolUse` instruction to hand off immediately. |
| `MEMENTO_GOVERNOR_BLOCK_TOKENS` | `400000` | `Stop` block threshold. |
| `MEMENTO_GOVERNOR_HANDOFF_PATH` | `MEMENTO_HANDOFF.md` | Absolute path or path relative to the hook payload's project `cwd`. |

Thresholds must be positive and ordered hygiene ≤ handoff ≤ block. Invalid
configuration fails open. The generated command captures the explicit enable
decision; make threshold/path variables visible to the Claude Code process
(restart it after changing persistent environment variables).

## Activation procedure

1. Set `MEMENTO_GOVERNOR_ENABLED=1` in the environment used to start the
   collector and restart the collector so its ordinary managed-hook reconcile
   writes the two governor entries into `~/.claude/settings.json`.
2. Optionally set the threshold and handoff-path variables above for the
   Claude Code process, then restart Claude Code.
3. Confirm the project handoff document has a Markdown **section heading**
   that names the active session id, for example
   `## Current session 9d9aca8e-427c-480a-a648-f9ab2e13a29e`. The production
   `memento-run-4` validation on 2026-08-29 found that a body-text mention did
   not satisfy Stop verification; use the heading form rather than relying on
   a prose mention. The Stop guard accepts neither mere file existence nor a
   section for another session.

   The current source matcher identifies headings and searches the text through
   the next heading, so it also recognizes an exact, delimiter-bounded id in a
   heading's body. That is broader than the production observation above; no
   governor logic changed in 0.0.55, and heading placement is the durable
   operational contract until that environment discrepancy is independently
   resolved.
4. To deactivate, remove or set `MEMENTO_GOVERNOR_ENABLED` false and restart
   the collector. Its next reconcile removes only its own governor entries;
   existing Memento pending-question hooks and user hooks remain intact.

## Non-changes

- No live service, existing Claude settings file, or sidecar binary is changed
  by this source release; the updated managed registration runs only when an
  operator starts the collector with explicit activation.
- No server/API behavior, queue behavior, or transcript contents are changed.
- No whole-transcript read occurs: unreadable, malformed, missing-usage, or
  missing-transcript input exits successfully with no hook output.
- No `PreToolUse` enforcement is installed. Emergency work can continue until
  the guarded Stop point.

## Verification

- Focused governor/runner registration tests: `28 passed in 2.49s`.
- Existing Claude pending-hook tests: `39 passed in 4.00s`.
- Full collector suite: `355 passed, 2 skipped, 169 subtests passed in 36.69s`.
