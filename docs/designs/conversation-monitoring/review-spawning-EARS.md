# Review Spawning - EARS

**Parent LLD**: ./LLD.md

## Threshold Triggering

- [x] **CM-RS-001**: When the user-message counter minus the last review user-message count reaches `review_threshold` (default 5), the system shall trigger a review spawn. *(Unit changed 2026-09-04: counts USER messages/exchanges, not assistant turns — see CM-TC-007. The check runs at the assistant-completion exchange boundary so reviews cover complete exchanges.)*
- [x] **CM-RS-002**: After triggering a threshold-based review, the system shall record the current user-message count as `lastReviewUserMsg`.

## Spawn Execution

- [x] **CM-RS-003**: When a review is triggered, the system shall check that the buffer is non-empty and no review is in progress before proceeding.
- [x] **CM-RS-004**: If the buffered message text contains the string "# Autolearn Review", the system shall skip the review spawn (depth guard against review-of-review).
- [x] **CM-RS-005**: The system shall set `reviewInProgress` to true before spawning and reset it to false after completion (success or failure).

## Review Formatting

- [x] **CM-RS-006**: The system shall format the buffered messages into a markdown document with Context section (project name, date, turn count), Instructions section, and Conversation section.
- [x] **CM-RS-007**: The system shall write the formatted review markdown to `~/.autolearn/reviews/review-{timestamp}.md`.

## Subprocess Spawning

- [x] **CM-RS-008**: The system shall spawn `opencode run <reviewMarkdown> --agent autolearn-reviewer --title "autolearn review"` as a detached subprocess.
- [x] **CM-RS-009**: The system shall set the `AUTOLEARN_REVIEWER=1` environment variable on the spawned subprocess to prevent recursive turn counting.
- [x] **CM-RS-010**: The spawned subprocess stdout and stderr shall be ignored (detached mode).

## Error Handling

- [x] **CM-RS-011**: If the review spawn fails, the system shall save the formatted review markdown to `~/.autolearn/review-failed-{timestamp}.md` for manual inspection.
- [x] **CM-RS-012**: If the review spawn fails, the system shall log the error message to the console with `[autolearn]` prefix.

## Observations Logging

- [x] **CM-RS-013**: When a review is spawned, the system shall append a JSON observation record to `~/.autolearn/observations.jsonl` with type "review_spawned", the message count, and the review file path.

## Stale Review Cleanup

- [x] **CM-RS-014**: After each review spawn, the system shall scan `~/.autolearn/reviews/` and delete files older than `stale_after_days` (default 30).

## Observations File Maintenance

- [x] **CM-RS-015**: The system shall trim `~/.autolearn/observations.jsonl` to a maximum of 1000 lines, discarding the oldest entries.

## Exit Review

- [ ] **CM-RS-016**: When the OpenCode process is about to exit (beforeExit event), the system shall spawn a final review if the buffer contains more than 2 messages.
- [ ] **CM-RS-017**: When the process receives SIGINT or SIGTERM, the system shall spawn a final review if the buffer contains more than 2 messages.
- [ ] **CM-RS-018**: The exit review shall use a synchronous spawn (write file + detached subprocess) to ensure the review is dispatched before the process terminates.
- [ ] **CM-RS-019**: The exit review shall not block or delay process shutdown.

## Cross-Process Throttle

- [x] **CM-RS-020**: The system shall gate every review spawn through a cross-process throttle that denies (a) a review whose Conversation-section hash matches the last committed review, (b) a spawn within `min_interval_ms` (default 3 min) of the last committed review, and (c) spawns beyond `max_reviews_per_day` (default 60) per calendar day.
- [x] **CM-RS-021** (2026-09-16, issue #14): When a plugin shell speculates on a review before clearing its buffer, the system shall check the throttle in peek mode (`commit: false`) so no lock is written; `runReviewSubprocess` shall be the single committing gate. A committing pre-check wrote the lock, and the authoritative gate then matched its own hash and suppressed the spawn.

## Related Documents

- [Conversation Monitoring LLD](./LLD.md)
