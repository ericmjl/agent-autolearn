# Conversation Monitoring - Low-Level Design

**Created**: 2026-06-05
**HLD Link**: ../high-level-design.md

> **OpenCode v2 (beta) note**: this LLD describes the v1 surface
> (`plugin/autolearn.js`, registered under the `plugin` config key). The v2
> shell (`plugin/autolearn-v2.js`, registered under `plugins`) implements the
> same loop against the v2 event stream: user text via
> `session.inbox.enqueued` (`item.payload.text`), assistant turns via
> `session.text.ended` + `session.step.ended`, idle via
> `session.execution.succeeded|failed|interrupted` boundaries, per-session
> state (the v2 stream is location-scoped, so no global primary-instance
> guard), and reviewer-session detection by agent/title in addition to the
> env guard. Shared logic lives in `plugin/autolearn-core.mjs`; reviews spawn
> through the version-aware wrapper (`opencode`/`opencode2`, with
> `opencode2 api delete` session cleanup). v2 has no exit reviews — the
> service outlives TUI sessions.

## Overview

The autolearn.js plugin hooks into OpenCode session events to count conversation turns, buffer messages (with redaction), detect idle periods, and spawn review subagents at configurable thresholds. This is the entry point for all learning — no monitoring means no reviews.

## Context

Per the HLD, the plugin runs in-process within the OpenCode runtime. It receives events through the `event` handler and manages a per-plugin-instance state (turn count, buffer, review lock). A single plugin instance is created per OpenCode session.

## Event Flow

```
message.part.delta ──→ accumulate text in messageTexts map
                            │
message.updated ─────→ finalize text, determine role
                            │
                      ┌─────▼──────┐
                      │ role=user? │───→ buffer user message (≤1000 chars)
                      │            │
                      │ role=asis? │───→ buffer assistant message (≤2000 chars)
                      │            │     increment turnCount
                      │            │     check threshold
                      └─────┬──────┘
                            │
                    turnCount - lastReviewTurn ≥ threshold?
                            │
                      ┌─────▼──────┐
                      │ spawnReview│
                      └────────────┘

session.idle ──→ buffer.length > 2 AND cooldown elapsed?
                      │
                ┌─────▼──────┐
                │ spawnReview│
                └────────────┘

process.beforeExit / SIGINT / SIGTERM ──→ buffer.length > 2?
                                                │
                                          ┌─────▼──────┐
                                          │ spawnExit  │
                                          │ Review     │
                                          └────────────┘
```

## Data Structures

### Per-instance state

| Field | Type | Initial | Description |
|-------|------|---------|-------------|
| turnCount | number | 0 | Total assistant turns since session start |
| lastReviewTurn | number | 0 | turnCount at last review spawn |
| lastIdleReview | number | 0 | timestamp (ms) of last idle-triggered review |
| buffer | Array<Message> | [] | Buffered messages for next review |
| currentSessionId | string\|null | null | Current session ID |
| reviewInProgress | boolean | false | Lock to prevent concurrent spawns |
| messageTexts | Map<id, text> | empty | Accumulated text per message |
| messageRoles | Map<id, role> | empty | Finalized role per message |

### Message shape (buffered)

| Field | Type | Description |
|-------|------|-------------|
| role | "user" \| "assistant" | Message sender |
| content | string | Redacted, truncated text |
| timestamp | string | ISO 8601 |

## Configuration

Loaded from `~/.autolearn/config.yaml`:

| Key | Default | Description |
|-----|---------|-------------|
| review_threshold | 5 | Assistant turns between reviews |
| max_conversation_buffer | 50 | Max messages kept in buffer |
| session_review_on_idle | true | Enable idle-triggered reviews |
| idle_cooldown_ms | 300000 | Minimum ms between idle reviews (5 min) |

## Secret Redaction

The `redact()` function strips secrets using regex before buffering:

```
/(api[_-]?key|token|secret|password|authorization|credentials?|auth)(["\s:=]+)([A-Za-z]+\s+)?([A-Za-z0-9_\-/.+=]{8,})/gi
```

Replaces the captured secret value with `[REDACTED]`.

## Truncation

- User messages: truncated to 1000 characters
- Assistant messages: truncated to 2000 characters
- Both are truncated before redaction is applied

## Review Spawning

When triggered (threshold or idle):

1. Check `buffer.length > 0` and `!reviewInProgress`
2. Guard against review loops: skip if buffer text contains `"# Autolearn Review"` heading
3. Format the review markdown speculatively and peek at the throttle (`throttleCheck(md, false)`) — if denied (busy window / duplicate), keep the buffer intact so the content rides the next trigger
4. Set `reviewInProgress = true`
5. Capture buffer, clear it
6. `runReviewSubprocess()` re-checks the throttle as the single committing gate (writes the lock on a pass), writes to `~/.autolearn/reviews/review-{Date.now()}.md`
7. Spawn `opencode run <reviewMd> --agent autolearn-reviewer` with `AUTOLEARN_REVIEWER=1` env
8. Log observation to `observations.jsonl`
9. Clean stale review files
10. Set `reviewInProgress = false`

## Stale Review Cleanup

After each spawn, scan `~/.autolearn/reviews/` for files older than `stale_after_days` (default 30) and delete them.

## Guard Mechanism

- `AUTOLEARN_REVIEWER=1` env var: plugin returns empty hooks, preventing recursive turn counting inside review subagents.
- `globalThis[Symbol.for("opencode:autolearn:<directory>")]`: prevents double-initialization if the plugin is loaded twice for the same directory. Keyed per directory (issue #14): opencode v1 loads the plugin once per directory in a single process, and a single global key silenced every instance except the first-loaded one — often not the active project. v2's location-scoped event stream needs no guard.
- Buffer depth check: if formatted review text contains the review heading, the spawn is skipped (catches review-of-review scenarios).

## Memory Instructions Injection

On plugin load, `injectInstructions()` reads `~/.config/opencode/opencode.json` and adds `~/.autolearn/memory.md` to the `instructions` array if not already present. This ensures the agent loads memory into context every session.

## Observations Log

Appended to `~/.autolearn/observations.jsonl`:

```json
{"type":"review_spawned","message_count":10,"review_file":"/path/to/review.md","timestamp":"...","project":"my-project"}
```

Trimmed to 1000 lines max (oldest entries dropped).

## Exit Review

OpenCode does not emit a `session.ended` event. To ensure buffered conversation is reviewed even when the session ends without hitting the turn threshold, the plugin registers process-level exit handlers.

### Triggers

| Trigger | Event | Async-safe? |
|---------|-------|-------------|
| Graceful exit | `process.on('beforeExit')` | Yes |
| Ctrl+C | `process.on('SIGINT')` | No — must be sync |
| Kill signal | `process.on('SIGTERM')` | No — must be sync |

### Exit Review Flow

1. Check `buffer.length > 2` (same minimum as idle review)
2. Format review markdown
3. Write to `~/.autolearn/reviews/review-exit-{timestamp}.md` (synchronous)
4. Spawn `opencode run` with `detached: true` (fire-and-forget)
5. For signal handlers, call `process.exit()` after spawn to ensure shutdown

The key constraint: signal handlers must be synchronous. `Bun.spawn` with `detached: true` returns immediately and the child process survives parent exit, so this is safe.

### Guard

Exit handlers are only registered in the primary (non-reviewer) plugin instance. The `AUTOLEARN_REVIEWER=1` check runs before handler registration.

## Edge Cases

1. **Plugin loaded in reviewer session**: `AUTOLEARN_REVIEWER=1` check causes early return — no hooks registered.
2. **Empty buffer at review time**: `spawnReview()` returns immediately.
3. **Review spawn failure**: Error logged, review markdown saved as `review-failed-{timestamp}.md` for manual inspection.
4. **Config file missing**: `parseConfig()` returns sensible defaults.
5. **opencode.json missing/unparseable**: `injectInstructions()` silently skips.

## Dependencies

- **OpenCode plugin API**: `ctx.client`, `ctx.directory`, `ctx.worktree`, event system
- **Bun runtime**: `Bun.spawn()` for subprocess
- **Node.js fs module**: File I/O for reviews, config, observations
- **autolearn-reviewer agent**: The spawned subprocess target

## Related Documents

- [High-Level Design](../../high-level-design.md)
- [Turn Counting & Buffering EARS](./turn-counting-EARS.md)
- [Review Spawning EARS](./review-spawning-EARS.md)
