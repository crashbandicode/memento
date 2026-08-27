# Phase 5 gate — soak-2 report (2026-08-27 21:42–22:47 UTC)

Steady-state soak after the FUM short-circuit deploy (f4197a3d16 + cbd13e8575,
drain started 20:56:42Z; clock restarted 21:42Z after the yoga Aug-13
chunk-loop recovery burst, itself attributed and archived in the handoff doc).

## Verdict summary
- 850 frames handled, 736 raw-committed (86.6%); zero drain/projector errors;
  two clean restart-recovery drills same day (20:22Z, 20:56Z).
- Per-reason RATE bound (<1/min): ALL PASS — claude sidecar pairing 0.78/min,
  cursor projection reordering 0.11/min, stable-identity 0.02/min, residual
  history reconcile 0.20/min. No unknown reasons appeared.
- Frame-share bound (<2%): FAILS as literally written — claude pairing 10.2%
  (87/850; night-time denominator ~13 frames/min with orchestrator-subagent
  traffic dominating), residual history reconcile 2.2% (19/850; history-laden
  docs incl. the Aug-13 recovery tail — the Shape A(b) work the shapes report
  deferred as optional).
- Recommendation surfaced to operator: adopt per-reason rate bounds + no
  unknown reasons (all pass), adding history-reconcile-on-recovered-docs to
  the enumerated legacy-forever set; or re-run the soak on daytime traffic
  mix for the literal % bound.

## Raw counter lines (windows with any fallback; clean windows omitted had
legacy_fallback_chains_by_reason={})
```text
2026-08-27 21:39:28,636 INFO ingest_spool Realtime raw-writer outcomes over 71.5s: total_handled_chains=12 total_handled_frames=15 raw_committed_chains=7 raw_committed_frames=7 legacy_fallback_chains_by_reason={'authoritative rebuild/history needs legacy reducer': 5} legacy_fallback_frames_by_reason={'authoritative rebuild/history needs legacy reducer': 8}
2026-08-27 21:40:41,552 INFO ingest_spool Realtime raw-writer outcomes over 68.3s: total_handled_chains=8 total_handled_frames=9 raw_committed_chains=6 raw_committed_frames=6 legacy_fallback_chains_by_reason={'authoritative rebuild/history needs legacy reducer': 2} legacy_fallback_frames_by_reason={'authoritative rebuild/history needs legacy reducer': 3}
2026-08-27 21:42:04,546 INFO ingest_spool Realtime raw-writer outcomes over 79.9s: total_handled_chains=7 total_handled_frames=7 raw_committed_chains=7 raw_committed_frames=7 legacy_fallback_chains_by_reason={} legacy_fallback_frames_by_reason={}
2026-08-27 21:43:09,556 INFO ingest_spool Realtime raw-writer outcomes over 62.0s: total_handled_chains=10 total_handled_frames=10 raw_committed_chains=10 raw_committed_frames=10 legacy_fallback_chains_by_reason={} legacy_fallback_frames_by_reason={}
2026-08-27 21:44:16,502 INFO ingest_spool Realtime raw-writer outcomes over 63.9s: total_handled_chains=9 total_handled_frames=11 raw_committed_chains=7 raw_committed_frames=7 legacy_fallback_chains_by_reason={'authoritative rebuild/history needs legacy reducer': 2} legacy_fallback_frames_by_reason={'authoritative rebuild/history needs legacy reducer': 4}
2026-08-27 21:45:19,726 INFO ingest_spool Realtime raw-writer outcomes over 60.2s: total_handled_chains=17 total_handled_frames=24 raw_committed_chains=5 raw_committed_frames=5 legacy_fallback_chains_by_reason={'Claude transcript/sidecar pairing needs the legacy reducer': 10, 'authoritative rebuild/history needs legacy reducer': 2} legacy_fallback_frames_by_reason={'Claude transcript/sidecar pairing needs the legacy reducer': 17, 'authoritative rebuild/history needs legacy reducer': 2}
2026-08-27 21:46:23,063 INFO ingest_spool Realtime raw-writer outcomes over 60.2s: total_handled_chains=17 total_handled_frames=27 raw_committed_chains=5 raw_committed_frames=6 legacy_fallback_chains_by_reason={'Claude transcript/sidecar pairing needs the legacy reducer': 12} legacy_fallback_frames_by_reason={'Claude transcript/sidecar pairing needs the legacy reducer': 21}
2026-08-27 21:47:52,614 INFO ingest_spool Realtime raw-writer outcomes over 85.0s: total_handled_chains=3 total_handled_frames=3 raw_committed_chains=3 raw_committed_frames=3 legacy_fallback_chains_by_reason={} legacy_fallback_frames_by_reason={}
2026-08-27 21:48:57,416 INFO ingest_spool Realtime raw-writer outcomes over 61.7s: total_handled_chains=4 total_handled_frames=4 raw_committed_chains=4 raw_committed_frames=4 legacy_fallback_chains_by_reason={} legacy_fallback_frames_by_reason={}
2026-08-27 21:51:21,738 INFO ingest_spool Realtime raw-writer outcomes over 136.6s: total_handled_chains=6 total_handled_frames=7 raw_committed_chains=1 raw_committed_frames=1 legacy_fallback_chains_by_reason={'authoritative rebuild/history needs legacy reducer': 5} legacy_fallback_frames_by_reason={'authoritative rebuild/history needs legacy reducer': 6}
2026-08-27 21:52:38,536 INFO ingest_spool Realtime raw-writer outcomes over 73.7s: total_handled_chains=4 total_handled_frames=4 raw_committed_chains=4 raw_committed_frames=4 legacy_fallback_chains_by_reason={} legacy_fallback_frames_by_reason={}
2026-08-27 21:53:45,669 INFO ingest_spool Realtime raw-writer outcomes over 64.0s: total_handled_chains=3 total_handled_frames=3 raw_committed_chains=3 raw_committed_frames=3 legacy_fallback_chains_by_reason={} legacy_fallback_frames_by_reason={}
2026-08-27 21:54:57,240 INFO ingest_spool Realtime raw-writer outcomes over 66.9s: total_handled_chains=7 total_handled_frames=9 raw_committed_chains=4 raw_committed_frames=4 legacy_fallback_chains_by_reason={'authoritative rebuild/history needs legacy reducer': 3} legacy_fallback_frames_by_reason={'authoritative rebuild/history needs legacy reducer': 5}
2026-08-27 21:56:36,644 INFO ingest_spool Realtime raw-writer outcomes over 94.7s: total_handled_chains=7 total_handled_frames=9 raw_committed_chains=4 raw_committed_frames=5 legacy_fallback_chains_by_reason={'Cursor projection reordering needs the legacy reducer': 1, 'authoritative rebuild/history needs legacy reducer': 1, 'stable-identity relocation/alias selection needs the legacy reducer': 1} legacy_fallback_frames_by_reason={'Cursor projection reordering needs the legacy reducer': 1, 'authoritative rebuild/history needs legacy reducer': 2, 'stable-identity relocation/alias selection needs the legacy reducer': 1}
2026-08-27 21:57:55,027 INFO ingest_spool Realtime raw-writer outcomes over 75.3s: total_handled_chains=4 total_handled_frames=6 raw_committed_chains=3 raw_committed_frames=5 legacy_fallback_chains_by_reason={'Cursor projection reordering needs the legacy reducer': 1} legacy_fallback_frames_by_reason={'Cursor projection reordering needs the legacy reducer': 1}
2026-08-27 21:59:35,012 INFO ingest_spool Realtime raw-writer outcomes over 95.3s: total_handled_chains=4 total_handled_frames=4 raw_committed_chains=2 raw_committed_frames=2 legacy_fallback_chains_by_reason={'Cursor projection reordering needs the legacy reducer': 2} legacy_fallback_frames_by_reason={'Cursor projection reordering needs the legacy reducer': 2}
2026-08-27 22:01:35,358 INFO ingest_spool Realtime raw-writer outcomes over 114.1s: total_handled_chains=5 total_handled_frames=5 raw_committed_chains=2 raw_committed_frames=2 legacy_fallback_chains_by_reason={'Cursor projection reordering needs the legacy reducer': 3} legacy_fallback_frames_by_reason={'Cursor projection reordering needs the legacy reducer': 3}
2026-08-27 22:02:56,283 INFO ingest_spool Realtime raw-writer outcomes over 76.2s: total_handled_chains=2 total_handled_frames=2 raw_committed_chains=2 raw_committed_frames=2 legacy_fallback_chains_by_reason={} legacy_fallback_frames_by_reason={}
2026-08-27 22:04:13,665 INFO ingest_spool Realtime raw-writer outcomes over 74.2s: total_handled_chains=4 total_handled_frames=5 raw_committed_chains=4 raw_committed_frames=5 legacy_fallback_chains_by_reason={} legacy_fallback_frames_by_reason={}
2026-08-27 22:05:17,266 INFO ingest_spool Realtime raw-writer outcomes over 60.5s: total_handled_chains=14 total_handled_frames=22 raw_committed_chains=14 raw_committed_frames=22 legacy_fallback_chains_by_reason={} legacy_fallback_frames_by_reason={}
2026-08-27 22:06:22,014 INFO ingest_spool Realtime raw-writer outcomes over 61.6s: total_handled_chains=17 total_handled_frames=25 raw_committed_chains=17 raw_committed_frames=25 legacy_fallback_chains_by_reason={} legacy_fallback_frames_by_reason={}
2026-08-27 22:07:36,294 INFO ingest_spool Realtime raw-writer outcomes over 69.6s: total_handled_chains=10 total_handled_frames=14 raw_committed_chains=10 raw_committed_frames=14 legacy_fallback_chains_by_reason={} legacy_fallback_frames_by_reason={}
2026-08-27 22:08:46,835 INFO ingest_spool Realtime raw-writer outcomes over 67.4s: total_handled_chains=1 total_handled_frames=1 raw_committed_chains=1 raw_committed_frames=1 legacy_fallback_chains_by_reason={} legacy_fallback_frames_by_reason={}
2026-08-27 22:09:53,064 INFO ingest_spool Realtime raw-writer outcomes over 63.1s: total_handled_chains=14 total_handled_frames=23 raw_committed_chains=14 raw_committed_frames=23 legacy_fallback_chains_by_reason={} legacy_fallback_frames_by_reason={}
2026-08-27 22:11:02,690 INFO ingest_spool Realtime raw-writer outcomes over 66.5s: total_handled_chains=12 total_handled_frames=18 raw_committed_chains=12 raw_committed_frames=18 legacy_fallback_chains_by_reason={} legacy_fallback_frames_by_reason={}
2026-08-27 22:12:07,492 INFO ingest_spool Realtime raw-writer outcomes over 61.7s: total_handled_chains=14 total_handled_frames=33 raw_committed_chains=14 raw_committed_frames=33 legacy_fallback_chains_by_reason={} legacy_fallback_frames_by_reason={}
2026-08-27 22:13:12,390 INFO ingest_spool Realtime raw-writer outcomes over 61.8s: total_handled_chains=11 total_handled_frames=34 raw_committed_chains=11 raw_committed_frames=34 legacy_fallback_chains_by_reason={} legacy_fallback_frames_by_reason={}
2026-08-27 22:14:27,027 INFO ingest_spool Realtime raw-writer outcomes over 70.0s: total_handled_chains=18 total_handled_frames=70 raw_committed_chains=18 raw_committed_frames=70 legacy_fallback_chains_by_reason={} legacy_fallback_frames_by_reason={}
2026-08-27 22:15:42,253 INFO ingest_spool Realtime raw-writer outcomes over 72.1s: total_handled_chains=8 total_handled_frames=16 raw_committed_chains=8 raw_committed_frames=16 legacy_fallback_chains_by_reason={} legacy_fallback_frames_by_reason={}
2026-08-27 22:16:51,545 INFO ingest_spool Realtime raw-writer outcomes over 66.2s: total_handled_chains=25 total_handled_frames=45 raw_committed_chains=11 raw_committed_frames=20 legacy_fallback_chains_by_reason={'Claude transcript/sidecar pairing needs the legacy reducer': 14} legacy_fallback_frames_by_reason={'Claude transcript/sidecar pairing needs the legacy reducer': 25}
2026-08-27 22:18:34,950 INFO ingest_spool Realtime raw-writer outcomes over 97.1s: total_handled_chains=8 total_handled_frames=14 raw_committed_chains=0 raw_committed_frames=0 legacy_fallback_chains_by_reason={'Claude transcript/sidecar pairing needs the legacy reducer': 8} legacy_fallback_frames_by_reason={'Claude transcript/sidecar pairing needs the legacy reducer': 14}
2026-08-27 22:19:38,577 INFO ingest_spool Realtime raw-writer outcomes over 60.5s: total_handled_chains=6 total_handled_frames=9 raw_committed_chains=0 raw_committed_frames=0 legacy_fallback_chains_by_reason={'Claude transcript/sidecar pairing needs the legacy reducer': 6} legacy_fallback_frames_by_reason={'Claude transcript/sidecar pairing needs the legacy reducer': 9}
2026-08-27 22:20:58,890 INFO ingest_spool Realtime raw-writer outcomes over 77.2s: total_handled_chains=7 total_handled_frames=7 raw_committed_chains=7 raw_committed_frames=7 legacy_fallback_chains_by_reason={} legacy_fallback_frames_by_reason={}
2026-08-27 22:22:28,781 INFO ingest_spool Realtime raw-writer outcomes over 85.2s: total_handled_chains=8 total_handled_frames=8 raw_committed_chains=7 raw_committed_frames=7 legacy_fallback_chains_by_reason={'Claude transcript/sidecar pairing needs the legacy reducer': 1} legacy_fallback_frames_by_reason={'Claude transcript/sidecar pairing needs the legacy reducer': 1}
2026-08-27 22:23:34,468 INFO ingest_spool Realtime raw-writer outcomes over 62.5s: total_handled_chains=16 total_handled_frames=21 raw_committed_chains=16 raw_committed_frames=21 legacy_fallback_chains_by_reason={} legacy_fallback_frames_by_reason={}
2026-08-27 22:24:41,199 INFO ingest_spool Realtime raw-writer outcomes over 63.6s: total_handled_chains=17 total_handled_frames=24 raw_committed_chains=17 raw_committed_frames=24 legacy_fallback_chains_by_reason={} legacy_fallback_frames_by_reason={}
2026-08-27 22:25:46,834 INFO ingest_spool Realtime raw-writer outcomes over 62.5s: total_handled_chains=15 total_handled_frames=16 raw_committed_chains=15 raw_committed_frames=16 legacy_fallback_chains_by_reason={} legacy_fallback_frames_by_reason={}
2026-08-27 22:26:50,486 INFO ingest_spool Realtime raw-writer outcomes over 60.5s: total_handled_chains=16 total_handled_frames=17 raw_committed_chains=16 raw_committed_frames=17 legacy_fallback_chains_by_reason={} legacy_fallback_frames_by_reason={}
2026-08-27 22:27:55,741 INFO ingest_spool Realtime raw-writer outcomes over 62.1s: total_handled_chains=13 total_handled_frames=13 raw_committed_chains=13 raw_committed_frames=13 legacy_fallback_chains_by_reason={} legacy_fallback_frames_by_reason={}
2026-08-27 22:28:59,180 INFO ingest_spool Realtime raw-writer outcomes over 60.3s: total_handled_chains=19 total_handled_frames=20 raw_committed_chains=19 raw_committed_frames=20 legacy_fallback_chains_by_reason={} legacy_fallback_frames_by_reason={}
2026-08-27 22:30:02,447 INFO ingest_spool Realtime raw-writer outcomes over 60.1s: total_handled_chains=15 total_handled_frames=17 raw_committed_chains=15 raw_committed_frames=17 legacy_fallback_chains_by_reason={} legacy_fallback_frames_by_reason={}
2026-08-27 22:31:06,991 INFO ingest_spool Realtime raw-writer outcomes over 61.4s: total_handled_chains=10 total_handled_frames=14 raw_committed_chains=10 raw_committed_frames=14 legacy_fallback_chains_by_reason={} legacy_fallback_frames_by_reason={}
2026-08-27 22:32:28,950 INFO ingest_spool Realtime raw-writer outcomes over 77.2s: total_handled_chains=10 total_handled_frames=13 raw_committed_chains=10 raw_committed_frames=13 legacy_fallback_chains_by_reason={} legacy_fallback_frames_by_reason={}
2026-08-27 22:33:36,820 INFO ingest_spool Realtime raw-writer outcomes over 64.7s: total_handled_chains=8 total_handled_frames=10 raw_committed_chains=8 raw_committed_frames=10 legacy_fallback_chains_by_reason={} legacy_fallback_frames_by_reason={}
2026-08-27 22:34:41,604 INFO ingest_spool Realtime raw-writer outcomes over 61.6s: total_handled_chains=17 total_handled_frames=36 raw_committed_chains=17 raw_committed_frames=36 legacy_fallback_chains_by_reason={} legacy_fallback_frames_by_reason={}
2026-08-27 22:35:45,695 INFO ingest_spool Realtime raw-writer outcomes over 60.9s: total_handled_chains=23 total_handled_frames=40 raw_committed_chains=23 raw_committed_frames=40 legacy_fallback_chains_by_reason={} legacy_fallback_frames_by_reason={}
2026-08-27 22:36:49,769 INFO ingest_spool Realtime raw-writer outcomes over 60.9s: total_handled_chains=27 total_handled_frames=62 raw_committed_chains=27 raw_committed_frames=62 legacy_fallback_chains_by_reason={} legacy_fallback_frames_by_reason={}
2026-08-27 22:38:03,148 INFO ingest_spool Realtime raw-writer outcomes over 68.6s: total_handled_chains=15 total_handled_frames=17 raw_committed_chains=15 raw_committed_frames=17 legacy_fallback_chains_by_reason={} legacy_fallback_frames_by_reason={}
2026-08-27 22:39:15,547 INFO ingest_spool Realtime raw-writer outcomes over 69.2s: total_handled_chains=11 total_handled_frames=14 raw_committed_chains=11 raw_committed_frames=14 legacy_fallback_chains_by_reason={} legacy_fallback_frames_by_reason={}
2026-08-27 22:40:20,641 INFO ingest_spool Realtime raw-writer outcomes over 61.9s: total_handled_chains=10 total_handled_frames=11 raw_committed_chains=10 raw_committed_frames=11 legacy_fallback_chains_by_reason={} legacy_fallback_frames_by_reason={}
2026-08-27 22:41:28,024 INFO ingest_spool Realtime raw-writer outcomes over 64.2s: total_handled_chains=8 total_handled_frames=9 raw_committed_chains=8 raw_committed_frames=9 legacy_fallback_chains_by_reason={} legacy_fallback_frames_by_reason={}
2026-08-27 22:42:37,305 INFO ingest_spool Realtime raw-writer outcomes over 66.1s: total_handled_chains=14 total_handled_frames=17 raw_committed_chains=14 raw_committed_frames=17 legacy_fallback_chains_by_reason={} legacy_fallback_frames_by_reason={}
```

## Shape attribution (per-document forensics, added 2026-08-27 ~23:20Z)

Method: one completion receipt exists per drained frame (receipt.document_id);
878 receipts fall in [21:42, 22:47)Z across exactly NINE documents. Joined
against Postgres and the drain log (fallback WARNINGs carry per-chain frame
counts; each legacy ingest of a sub-threshold doc logs a Post-ingest line
naming tool/doc). Counter total was 850 frames — ~3% window-edge skew between
receipt timestamps and counter-window boundaries; ratios unaffected.

### Fallback volume: 114 frames, 4 reasons, THREE documents, zero unexplained
| Reason | Frames/chains | Document(s) | Evidence | Why allowed |
|---|---|---|---|---|
| Claude transcript/sidecar pairing | 87 / 51 | ONE doc: 068761ac — the Phase 5 Plan agent subagent transcript (fe4bdc0b.../subagents/agent-a8219f353e7676f9c.jsonl, butterbridge) | receipts(doc)=87 = counter bucket EXACTLY; all 87 Post-ingest lines name this doc; reason string has a single raise site, fired iff path matches the subagent transcript/sidecar pattern | Intentional legacy-forever: cross-file transcript+sidecar pairing is outside the raw writer single-source transaction by design (shapes report Shape C) |
| authoritative rebuild/history | 19 / 13 | ONE doc: 84a3e717 — the Aug-13 yoga codex rollout (261MB) during post-rebase catch-up | receipts(doc)=19 = counter bucket EXACTLY; the 13 WARNINGs' per-chain frame counts sum to exactly 19; mutation fingerprint: its recovered history rows went 178 -> 10 in-window (only legacy reconcile deletes recovered rows) | Shape A(b): user_history reconcile on a doc carrying legacy-era recovered rows — the full raw port was explicitly deferred as optional (shapes report §4) |
| Cursor projection reordering | 7 / 7 | ONE doc: 28bc8f69 — yoga Grok composer transcript ("Codex permission issues") | every WARNING immediately adjacent to a Post-ingest line naming this doc | Enumerated legacy-forever guard (row reorder) |
| stable-identity relocation/alias | 1 / 1 | same doc 28bc8f69 | WARNING at 21:55:47.985 adjacent to its Post-ingest at 21:55:48.057 | Enumerated legacy-forever guard |

### Positive control for today's FUM fix
The three fresh exec/MCP-driven codex sessions (terra-phase5 be18cf36 143
frames, sol-review-phase5 547cbfbd 110, "Count farm UI link sharers" 39cdeaab
371) all attach first_user_message metadata on every emission — the exact
shape that fell back on EVERY delta before commits f4197a3d16/cbd13e8575 —
and raw-committed 100% of their 624 frames (zero legacy evidence). The
claude_code main-session transcripts (2ec78baa 69, 050540a9 49, a1366e1b 21)
also raw-committed 100%.

### Conclusion
The 10.2% pairing share was ONE busy subagent transcript (the Plan agent
writing a long report), not a distributed coverage failure; the 2.2% history
share was ONE recovery-day doc. Every fallback frame maps to a deliberately
unsupported or explicitly deferred shape; there are zero unexplained shapes.
