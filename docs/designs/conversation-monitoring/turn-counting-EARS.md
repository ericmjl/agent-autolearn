# Turn Counting & Buffering - EARS

**Parent LLD**: ./LLD.md

## Turn Counting

- [x] **CM-TC-001**: The system shall increment the assistant-message counter once for each finalized assistant message. *(Observability counter only as of 2026-09-04: the review threshold unit is user messages — CM-TC-007.)*
- [x] **CM-TC-002**: When a message part delta arrives, the system shall accumulate the delta text into the message's text buffer.
- [x] **CM-TC-003**: When a message is finalized (message.updated event), the system shall determine the role and buffer the message content.
- [x] **CM-TC-004**: If the message role is "user", the system shall buffer the message content (truncated to 1000 characters) without incrementing the turn counter.
- [x] **CM-TC-005**: If the message role is "assistant", the system shall buffer the message content (truncated to 2000 characters) and increment the assistant-message counter.
- [x] **CM-TC-006**: The system shall redact secrets (API keys, tokens, passwords) from all buffered message content before storing.
- [x] **CM-TC-007** (2026-09-04): The system shall increment a user-message counter once per buffered user message (v2: `session.inbox.enqueued`; v1: user-role `message.updated`). The review threshold counts USER messages (exchanges), not assistant turns — one user message can produce many assistant turns in tool-heavy sessions, so turn-based thresholds fire mid-exchange on machine activity rather than interaction volume.

## Buffer Management

- [x] **CM-BUF-001**: The system shall maintain a buffer of recent messages for review formatting.
- [x] **CM-BUF-002**: If the buffer exceeds `max_conversation_buffer` messages (default 50), the system shall discard the oldest messages.
- [x] **CM-BUF-003**: When a review is spawned, the system shall capture the buffer contents and clear it for the next review cycle.

## Idle Detection

- [x] **CM-IDLE-001**: When a session.idle event fires and `session_review_on_idle` is true (default), the system shall consider spawning a review.
- [x] **CM-IDLE-002**: If the buffer contains fewer than 3 messages, the system shall not spawn an idle review.
- [x] **CM-IDLE-003**: If an idle review was spawned within the last `idle_cooldown_ms` milliseconds (default 300000 / 5 min), the system shall not spawn another idle review.
- [x] **CM-IDLE-004**: If a review is already in progress, the system shall not spawn an idle review.

## Memory Injection

- [x] **CM-MEM-001**: When the plugin loads, the system shall add the memory instruction path to the opencode.json instructions array if not already present. *(Superseded by MI-CMP-009: the injected path is now `~/.autolearn/personas/default/memory.context.md`; legacy `memory.md` paths are actively removed.)*

## Reviewer Guard

- [x] **CM-GUARD-001**: If the `AUTOLEARN_REVIEWER` environment variable is set to "1", the system shall not register any event hooks.
- [x] **CM-GUARD-002**: The system shall set a global symbol guard to prevent double-initialization if the plugin module is loaded twice. *(Amended 2026-09-16, issue #14: the guard symbol is keyed by directory — `Symbol.for("opencode:autolearn:<directory>")`. A single global key let the first-loaded project's instance monitor while the active project's instance hit the guard and returned empty hooks, since opencode v1 loads the plugin once per directory in one process. Per-directory keys keep the same-directory double-load protection and make every monitored project's instance independently primary. v2 is unaffected: its event stream is location-scoped with no guard.)*

## Related Documents

- [Conversation Monitoring LLD](./LLD.md)
