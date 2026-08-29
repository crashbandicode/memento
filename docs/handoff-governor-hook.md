# Claude Code handoff governor hook

## Purpose

Long autonomous Claude Code orchestrator sessions need a durable handoff before
their prompt prefix consumes the context window. The governor is a managed,
inert-by-default pair of Claude Code hooks that provides a staged nudge and a
last-resort Stop guard.

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
3. Confirm the project handoff document has a Markdown section that names the
   active session id. The Stop guard accepts neither mere file existence nor a
   section for another session.
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

- Focused governor hook tests: `13 passed in 0.24s`.
- Existing Claude pending-hook tests: `39 passed in 3.28s`.
- Full collector suite: `333 passed, 2 skipped, 169 subtests passed in 30.07s`.
