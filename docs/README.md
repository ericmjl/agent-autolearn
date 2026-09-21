# Autolearn Design Docs

## Index

Start with the [High-Level Design](./high-level-design.md) — it has a status column on the Feature Breakdown table and marks each Key Decision as `shipped` or `planned`.

### Shipped features

| Design | LLD | EARS | Code |
|--------|-----|------|------|
| Conversation Monitoring | [LLD](./designs/conversation-monitoring/LLD.md) | [turn-counting](./designs/conversation-monitoring/turn-counting-EARS.md), [review-spawning](./designs/conversation-monitoring/review-spawning-EARS.md) | `plugin/autolearn.js` + `plugin/autolearn-v2.js` (core: `autolearn-core.mjs`) |
| Knowledge Store | [LLD](./designs/knowledge-store/LLD.md) | [memory-management](./designs/knowledge-store/memory-management-EARS.md), [observations-logging](./designs/knowledge-store/observations-logging-EARS.md) | `autolearn.py` (memory, user, observations) |
| Skill Management | [LLD](./designs/skill-management/LLD.md) | [skill-crud](./designs/skill-management/skill-crud-EARS.md), [skill-lifecycle](./designs/skill-management/skill-lifecycle-EARS.md) | `autolearn.py` (skill, curator) |
| Review Agent | [LLD](./designs/review-agent/LLD.md) | [conversation-evaluation](./designs/review-agent/conversation-evaluation-EARS.md), [action-execution](./designs/review-agent/action-execution-EARS.md) | `skills/autolearn/SKILL.md` + `skills/autolearn/references/reviewer.md` |
| Session Search | [LLD](./designs/session-search/LLD.md) | — | `autolearn.py` (search) |
| Behavioral Escalation | (covered in the autolearn skill, observer reference) | — | `skills/autolearn/references/observer.md`, `improve.py` |
| Sync Encryption | [LLD](./designs/sync/encryption-LLD.md) | [EARS](./designs/sync/encryption-EARS.md) | `scripts/sync_crypto.py`, `autolearn.py` (sync) |
| Sync Protocol | [LLD](./designs/sync/protocol-LLD.md) | [EARS](./designs/sync/protocol-EARS.md) | `autolearn.py` (sync), `sync-server/`, `sync-convex/`, `plugin/autolearn.js` + `plugin/autolearn-v2.js` (core: `autolearn-core.mjs`) |
| Multi-Persona | [LLD](./designs/sync/persona-LLD.md) | [EARS](./designs/sync/persona-EARS.md) | `autolearn.py` (persona), `plugin/autolearn.js` + `plugin/autolearn-v2.js` (core: `autolearn-core.mjs`) |

### Deferred (designed, not yet implemented)

- `sync rotate-key` (SYNC-ENC-013) — re-encrypt all blobs with new password
- `sync pull --interactive` (SYNC-PROTO-011) — per-file conflict resolution prompt
- `sync push` all-personas iteration (SYNC-PER-012/013) — currently pushes active persona only
- Project-level `.autolearn-persona` file (SYNC-PER-014) — project-specific persona mapping
- Salt auto-bootstrap — Phase 1 requires manual `scp ~/.autolearn/.encryption_salt` to new machines
- Certified Procedures correlation-falsification (CP-FAL-005) — probabilistic, suggestion-only, deferred behind the deterministic path

### In flight

- [Certified Procedures LLD](./designs/certified-procedures/LLD.md) (+ [EARS](./designs/certified-procedures/certified-procedures-EARS.md)) — falsify skills against ground-truth tool outcomes (Loop 1) + golden-path shortcut extraction (Loop 2), on a shared `outcomes.py` spine. See HLD Decisions 10–12.

> Note: Memory Insight (`docs/designs/memory-insight/`) is **shipped** (registry,
> retention, composer, shift detector, inspector UI all live) — the HLD status
> rows reflect this.

## Conventions

- **LLD** (Low-Level Design): describes a single component's architecture, data structures, and edge cases. One per feature area.
- **EARS** (Easy Approach to Requirements Syntax): testable requirement specifications using the `WHEN <trigger> THE SYSTEM SHALL <behavior>` form. Each LLD links to its EARS files.
- Code references in EARS use `@spec <ID>` tags that map back to LLD section IDs.
