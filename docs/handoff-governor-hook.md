# Cross-engine handoff advisory hook

## Purpose

Long autonomous Claude, Codex, and Cursor orchestrator sessions benefit from a
durable handoff before their prompt prefix consumes the context window. The
governor is an inert-by-default managed `PostToolUse` advisory. It never blocks
work or initiates a handoff without the operator's explicit approval.

## Hook execution and latency

On Windows collector 0.0.60, managed hook registrations invoke the
dedicated `memento-hook-runner.exe`, not the large onefile collector sidecar.
It is a small PyInstaller **onedir** bundle, copied on collector startup from
the desktop application's packaged resource to a versioned directory such as
`%LOCALAPPDATA%\Memento\hooks\0.0.60\`. Its `_internal` directory stays beside
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
never deleted in the same pass. Sweeping is disabled unless
`MEMENTO_HOOK_RUNNER_RETENTION_HOURS` is explicitly set to a finite positive
value. This opt-in is required because Claude, Cursor, and Codex cache absolute
hook commands for the life of a session, which may outlive a collector rollout.
When enabled, only collector-daemon startup—not the hook installer command—may
sweep a marker older than the configured interval. The current/registered
version and legacy-sidecar fallback state are never marked or swept.

For an aged, unregistered version, deletion is executable-gated: the first
filesystem mutation is removal of `memento-hook-runner.exe`. Windows refuses
to remove a live process image, so any failure leaves the entire directory
unchanged for a later daemon start. Only after that executable removal succeeds
may the remaining directory be removed; a residual cleanup failure is harmless
because the executable is already gone and the retired version can never launch
again. The daemon/`run` sidecar remains the existing onefile artifact.

Codex's native Windows hook host launches command strings through `cmd.exe`.
Collector 0.0.60 therefore emits an unquoted leading executable token for a
Codex registration when the resolved runner path contains neither whitespace
nor command-shell metacharacters. This avoids Codex 0.152's exit-1 failure on
otherwise valid commands beginning with a quoted executable path. Claude Code
registrations retain their existing quoting, and unsafe paths remain quoted.

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
2. **Stages latch once per session and threshold.** The `PostToolUse` `*` hook
   records delivered thresholds in `~/.memento/governor/<session_id>.json`.
   This prevents duplicate developer context on every tool call. At the former
   cutoff stage, the injected policy tells the agent to add a short handoff
   recommendation to each final response until the operator approves,
   snoozes, or disables reminders.
3. **The operator owns the decision.** There is no managed `Stop` hook and no
   blocking response. The agent keeps executing the active request. Silence,
   acknowledgment, or an instruction to continue is not handoff approval; an
   explicit operator go-ahead in response to the recommendation is required.
   Requests to remind later or not at all are honored.
4. **Activation is explicit.** The governor is off by default. The collector
   adds its managed `PostToolUse` and `Stop` entries only when enabled, and the
   generated command carries `--enabled`; a fleet rollout alone therefore
   cannot activate the governor.
5. **Hook output is engine-specific.** Claude Code and Codex receive only their
   strict camelCase/common JSON fields. Cursor receives its native snake_case
   compatibility fields. Codex rejects unknown Stop and PostToolUse fields, so
   the runner never sends Cursor-only fields to a Codex payload.
6. **Cursor runs exactly one governor.** Current Cursor builds import the global
   Claude hook configuration. When that managed Claude governor is present, the
   collector removes its native `~/.cursor/hooks.json` copy so one tool
   completion cannot launch the frozen runner twice. A Cursor-only installation
   without the managed Claude hook retains the native `postToolUse` fallback.
   User entries are preserved and obsolete managed `stop` entries are removed.

## Configuration

| Variable | Default | Meaning |
| --- | ---: | --- |
| `MEMENTO_GOVERNOR_ENABLED` | off | Enables managed governor-hook registration when the collector reconciles Claude settings. Truthy values: `1`, `true`, `yes`, `on`. |
| `MEMENTO_GOVERNOR_HYGIENE_TOKENS` | `200000` | First `PostToolUse` context-hygiene nudge. |
| `MEMENTO_GOVERNOR_HANDOFF_TOKENS` | `350000` | Second `PostToolUse` instruction to hand off immediately. |
| `MEMENTO_GOVERNOR_REMINDER_TOKENS` | `400000` | Activates the recurring, operator-controlled recommendation policy. |
| `MEMENTO_GOVERNOR_BLOCK_TOKENS` | compatibility alias | Former cutoff variable; treated as the reminder threshold when the new variable is unset. It never blocks. |
| `MEMENTO_HOOK_RUNNER_RETENTION_HOURS` | unset (sweep off) | Enables deletion of retired versioned runner directories after this many hours. Must be finite and greater than zero. |

Thresholds must be positive and ordered hygiene ≤ handoff ≤ reminder. Invalid
configuration fails open. When the engine reports its context-window size and
the corresponding variable is not explicitly configured, the effective
hygiene/handoff/reminder defaults are capped at 75%/90%/95% of that window.
The handoff nudge tells the agent to continue the operator's current request;
the former cutoff becomes recurring advice rather than a veto. The generated
command captures the explicit enable decision; make threshold variables
visible to the agent process (restart it after changing persistent environment
variables).

## Activation procedure

1. Set `MEMENTO_GOVERNOR_ENABLED=1` in the environment used to start the
   collector and restart the collector so its ordinary managed-hook reconcile
   writes the advisory into `~/.claude/settings.json` and
   `~/.codex/hooks.json`. Cursor imports the managed Claude entry;
   `~/.cursor/hooks.json` is used only when that entry is unavailable.
2. Optionally set the threshold and handoff-path variables above for the
   Claude Code process, then restart Claude Code.
3. To deactivate, remove or set `MEMENTO_GOVERNOR_ENABLED` false and restart
   the collector. Its next reconcile removes only its own governor entries;
   existing Memento pending-question hooks and user hooks remain intact.

## Non-changes

- No live service, existing Claude settings file, or sidecar binary is changed
  by this source release; the updated managed registration runs only when an
  operator starts the collector with explicit activation.
- No server/API behavior, queue behavior, or transcript contents are changed.
- No whole-transcript read occurs: unreadable, malformed, missing-usage, or
  missing-transcript input exits successfully with no hook output.
- No `PreToolUse` or `Stop` enforcement is installed. Context pressure cannot
  stop a response or force a successor without operator authorization.

## Verification

See the release handoff for the exact collector, frozen-runner, and desktop
build gates executed for the current version.
