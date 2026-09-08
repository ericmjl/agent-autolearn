# OpenCode Autolearn - High-Level Design

**Created**: 2026-06-05
**Last updated**: 2026-08-29

> **Status legend** — Feature Breakdown and Key Design Decisions mark each item as
> `shipped` (implemented in `autolearn.py` / the plugin shells `autolearn.js` + `autolearn-v2.js`), `planned` (designed
> but not yet implemented), or `partial`. Sync (E2E encryption, Fastify + Convex
> backends, multi-persona, plugin auto-sync) is shipped. Deferred: `rotate-key`,
> interactive conflict resolution, salt auto-bootstrap, project-level persona mapping.
>
> **Memory Insight** (`docs/designs/memory-insight/`) is **shipped**: the registry
> (`memories.jsonl`), Ebbinghaus retention, the dynamic context composer, the
> recurring-preference shift detector, and the inspector UI are all live and wired
> into `autolearn.py` (see Decisions 8–9 and the Memory Insight LLD).
>
> **In flight** — *Certified Procedures* (`docs/designs/certified-procedures/`):
> the Schema-harness-inspired ability to falsify autolearn's own skills against
> ground-truth tool outcomes, and to shortcut roundabout workflows to a golden
> path. Two co-equal loops (falsify + efficiency) on a shared `outcomes.py` spine
> that indexes `opencode.db`'s tool-call + step-cost data the existing FTS5 index
> excludes. See Decisions 10–12 and the Certified Procedures LLD.
>
> **In flight** — *Wiki Layer* (`docs/designs/wiki-layer/`): the persistent
> pattern notebook between raw experience and skills, adapted from WikiSkill
> (arXiv 2608.27454). `wiki/patterns/` pages hold diagnosed patterns (trigger +
> root cause + fix + evidence) that have not yet recurred enough for a skill;
> the harness-written `skill-impact.md` ledger records every skill mutation and
> rejection (append-only, rejections kept forever); the reviewer reads the wiki
> before proposing and makes at most one skill change per review; skills carry
> `PURPOSE.md` provenance (backfilled for existing skills). Sync is explicitly
> out of scope (Decision 15). See Decision 15 and the Wiki Layer LLD.

## Problem Statement

AI coding agents repeat the same mistakes across sessions because they have no mechanism to learn from user corrections, preferences, and recurring patterns. Every session starts from scratch, forcing users to re-state the same preferences and re-correct the same behaviors.

## Goals

1. **Automatic learning** — Capture user corrections and preferences from conversation flow without requiring explicit "remember this" commands. _(shipped)_
2. **Behavioral escalation** — Detect when a correction recurs across projects and escalate it into persistent agent instructions (AGENTS.md). _(shipped via `self-improving-agent/improve.py`)_
3. **Skill evolution** — Allow the agent to create, patch, and retire its own skills based on observed patterns. _(shipped)_
4. **Zero-friction operation** — Work as a background plugin that requires no user intervention during normal operation. _(shipped)_
5. **Cross-machine sync** — Propagate learned knowledge between machines via an E2E-encrypted sync service. _(shipped — AES-256-GCM crypto, sync CLI, Fastify + Convex backends, multi-persona, plugin auto-sync. See Decisions 5–7 and `docs/designs/sync/`.)_

## Non-Goals

- **Cross-agent synchronization** — Each agent harness manages its own autolearn store; no sync between Claude Code, Copilot, etc.
- **Cloud-based memory (plaintext)** — All data stays local by default. Sync is optional and E2E-encrypted; the server never sees plaintext.
- **Model fine-tuning** — Autolearn changes agent behavior via instructions and skills, not model weights.
- **Self-improving-agent invocation boundary** — The `improve.py` observation/escalation CLI is bundled in this repo (under `skills/self-improving-agent/`) but is **not invoked directly by the plugin** — only the reviewer agent calls it via the SKILL.md protocol. This keeps the plugin thin and lets the reviewer decide when a correction is worth recording.
- **Real-time collaborative editing** — Single-writer per persona per machine at a time.
- **Cross-user persona sharing** — Personas are private to one user (future: team personas with key exchange).

## Target Users

- **Developers using OpenCode** who want their coding agent to improve over time without manual configuration.
- **Agent skill authors** who want skills that can be patched and extended by the autolearn system.

## Architecture Overview

```
Machine A                              Machine B
┌──────────────────────────────────┐   ┌──────────────────────────────────┐
│         OpenCode Runtime         │   │         OpenCode Runtime         │
│                                  │   │                                  │
│  ┌────────────────────────────┐  │   │  ┌────────────────────────────┐  │
│  │  autolearn plugin shells   │  │   │  │  autolearn plugin shells   │  │
│  │  (v1 .js / v2 -v2.js over  │  │   │  │  (v1 .js / v2 -v2.js over  │  │
│  │  autolearn-core.mjs)        │  │   │  │  autolearn-core.mjs)        │  │
│  │  Turn Counter → Buffer     │  │   │  │  Turn Counter → Buffer     │  │
│  │  → Idle → Review Spawner   │  │   │  │  → Idle → Review Spawner   │  │
│  └────────────┬───────────────┘  │   │  └────────────┬───────────────┘  │
│               │                  │   │               │                  │
│     autolearn-reviewer agent     │   │     autolearn-reviewer agent     │
│               │                  │   │               │                  │
│  ┌────────────▼───────────────┐  │   │  ┌────────────▼───────────────┐  │
│  │     autolearn.py (CLI)     │  │   │  │     autolearn.py (CLI)     │  │
│  │                            │  │   │  │                            │  │
│  │  memory/user/skill/curator │  │   │  │  memory/user/skill/curator │  │
│  │  sync push/pull (E2E enc)  │  │   │  │  sync push/pull (E2E enc)  │  │
│  └────────────┬───────────────┘  │   │  └────────────┬───────────────┘  │
│               │                  │   │               │                  │
│  ┌────────────▼───────────────┐  │   │  ┌────────────▼───────────────┐  │
│  │  ~/.autolearn/             │  │   │  │  ~/.autolearn/             │  │
│  │  personas/                 │  │   │  │  personas/                 │  │
│  │    default/                │  │   │  │    default/                │  │
│  │    work/                   │  │   │  │    work/                   │  │
│  │    personal/               │  │   │  │    personal/               │  │
│  └────────────────────────────┘  │   │  └────────────────────────────┘  │
└──────────────────────────────────┘   └──────────────────────────────────┘
                  │                                      │
                  └──────────────┬───────────────────────┘
                                 │  HTTPS (TLS)
                          ┌──────▼──────┐
                          │ Sync Server │
                          │ (opaque     │
                          │  encrypted  │
                          │  blobs only)│
                          │             │
                          │ Convex or   │
                          │ self-hosted │
                          └─────────────┘
```

## Key Design Decisions

### Decision 1: Plugin-based architecture (not standalone service)

**Choice**: OpenCode plugin that hooks into session events.

**Rationale**: Plugins have direct access to conversation events (message deltas, idle state) without requiring a separate process, network port, or API. The plugin is loaded in-process and can react to events in real time.

**Alternatives considered**:
- Standalone daemon with API: More complex, requires port management, adds network latency to event processing.
- Post-session script: Loses real-time idle detection and requires a separate trigger mechanism.

### Decision 2: Markdown-based data store

**Choice**: Plain markdown files (`memory.md`, `user-profile.md`) for persistent knowledge.

**Rationale**: OpenCode loads instruction files directly into agent context. Markdown files serve as both storage and context injection — no conversion needed. The agent reads the same file the user can edit.

**Alternatives considered**:
- SQLite: More queryable but not readable by the agent without tool calls. Adds a binary dependency.
- JSON: Structured but not human-readable and not directly loadable as instructions.

### Decision 3: Subprocess spawning for reviews

**Choice**: Spawn a separate `opencode run` subprocess for each review, with the `AUTOLEARN_REVIEWER=1` guard to prevent recursive review spawning.

**Rationale**: Reviews are expensive (full LLM call with skill loading). Running them in a subprocess isolates failures, prevents the main session from blocking, and naturally limits concurrency to one review at a time.

**Alternatives considered**:
- In-process background task: Risk of interfering with main session context, harder to isolate failures.
- Job queue: Over-engineered for a single-user local tool.

### Decision 4: Python CLI for data management

**Choice**: `autolearn.py` as a standalone Python script with PEP 723 inline metadata.

**Rationale**: The reviewer agent needs programmatic access to create/patch skills and update memory. A CLI is the simplest interface that both the agent (via bash) and the user (via terminal) can use. PEP 723 inline metadata means no virtualenv setup required — `uv run` handles it.

**Alternatives considered**:
- Node.js CLI: Would match the plugin language but the reviewer agent works better with Python for string processing and YAML manipulation.
- Direct file writes from the skill: Fragile, no validation, no deduplication logic.

### Decision 5: E2E-encrypted sync (zero-knowledge server) — _shipped_

**Choice**: Client-side AES-256-GCM encryption before syncing. The server stores only opaque ciphertext.

**Rationale**: Sync requires trusting a server with personal coding preferences and habits. E2E encryption means even the database operator (including the user viewing the Convex dashboard) cannot read the content. Master password → PBKDF2 → per-file encryption keys derived via HMAC chain. OS keychain stores the derived key for convenience.

**Alternatives considered**:
- Plaintext sync (trust the server): Simpler but unacceptable for privacy-sensitive learning data.
- Per-file symmetric keys without key hierarchy: Simpler but no isolation between personas.
- Asymmetric encryption (public key): Overkill for single-user data, adds complexity.

### Decision 6: Multi-persona knowledge stores — _shipped_

**Choice**: Each persona is an isolated subdirectory under `~/.autolearn/personas/{name}/` with its own complete set of files, synced independently.

**Rationale**: Developers operate in distinct contexts (work, personal, OSS) with different conventions and tooling. Mixing them into one memory store creates noise — a work-specific CI pattern isn't useful in personal projects. Personas are client-side namespaced (UUIDs on the server), so persona names like "work" never leave the machine.

**Alternatives considered**:
- Tags on entries instead of separate stores: Requires filtering on every read, no isolation.
- Multiple autolearn installations: Duplication, each needs its own plugin config.
- Single store with persona field: Leakage risk, complex filtering.

### Decision 7: Backend-agnostic sync API (Convex or self-hosted) — _shipped_

**Choice**: A thin sync API spec (push/pull/status) that any backend can implement. Ships with a Convex backend (managed, deployed under the project maintainer's account) and a self-hosted Fastify+SQLite backend (free, run on your own machine).

**Rationale**: Users should not be locked into a specific hosting provider. The managed Convex service is convenient (no server to operate), but fully self-hosting should be equally easy (Docker image or bare bun process). The CLI is backend-agnostic — it only needs a URL and API key.

**Alternatives considered**:
- Convex only: Lock-in, users must have a Convex account.
- Generic S3/object store: No conflict detection, no per-file metadata, no auth built in.
- Git-based sync: Merge conflicts on markdown bullet lists, requires git knowledge, no realtime.

### Decision 8: Store/view separation — registry + dynamic context composition — _shipped_

**Choice**: Replace the single static, 3000-char-capped `memory.md` with two
layers — a durable, unbounded `memories.jsonl` registry, and a per-session
`memory.context.md` generated by ranking records by relevance × retention into a
soft character budget.

**Rationale**: `memory.md` conflates *durability* (should this lesson survive?)
with *relevance* (should it be in this session's context?). A FIFO char cap
answers neither: it silently drops still-valid lessons (staleness) while their
reinforcement counters orphan in `strengths.json`, and it competes with the
Ebbinghaus curve's decay policy. Separating the store from the view lets
forgetting be driven by decay over an unbounded store, while the context window
gets the most useful (relevant + retained) tokens each session.

**Alternatives considered**:
- Keep `memory.md`, raise the cap: postpones staleness, does not fix the
  durability/relevance conflation or orphan strengths.
- SQLite-only store: more queryable but not directly loadable as instructions;
  the composer still emits the markdown view the agent reads.
- Embeddings for relevance: deferred — lexical Jaccard over topic tokens covers
  the recurring-preference case at zero dependency cost (see Decision 9).

### Decision 9: Ebbinghaus-governed forgetting, embedding-free — _shipped_

**Choice**: Each memory carries a decaying retention score
(`salience · e^(−λ·Δt) + σ·Σ(1/days_since_reinforcement)`); a record is evicted
only after staying below the cold tier for a grace period. Reinforcement
(boost), tiering, and contradiction detection all run on plain text + timestamps
— no vector embeddings anywhere in the lifecycle.

**Rationale**: Decay/strengthen/evict is orthogonal to retrieval. AgentMemory's
own retention engine and Jaccard contradiction detector are embedding-free
(embeddings only touch optional retrieval quality, where BM25-only still scores
86% R@5). Keeping autolearn embedding-free preserves its zero-new-deps,
`uv run`-only operation model and matches the user's "simple, agent-operable
tech" preference. Embeddings remain a future, optional upgrade to the composer's
relevance step alone.

**Alternatives considered**:
- Embeddings now: adds a model dependency + vector store for marginal gain on
  the recurring-preference detection that motivated the feature.
- Hand-rolled FIFO retention (status quo): causes the staleness and orphans this
  decision set out to fix.

### Decision 10: Falsify skills, not memories; against tool outcomes, not text — _in flight_

**Choice**: Certified Procedures targets **skills** (autolearn's program-analog)
and falsifies them against **ground-truth-bearing tool outcomes** indexed from
`opencode.db`'s `part` table (`tool` / `step-finish` rows), which the existing
FTS5 search index deliberately excludes (session-search DD4). It does **not**
backtest prose memories against prose snippets.

**Rationale**: The earlier "Certified Memory" concept was rejected on review.
Schema's backtest is valuable because it is *verifiable* (run the program →
compare to ground truth). A repo is not a closed deterministic system, so exact
replay is impossible — but `opencode.db` *does* carry structured
ground-truth-bearing evidence (106k+ tool parts incl. 2,485 errors; ~98k
step-finish token ledgers; 1,394 skill-load parts). Prose-vs-prose backtesting
retains none of Schema's verifiability and is polarity-blind and
drift-confounding; backtesting skills against tool outcomes does. The critical
epistemic rule: an outcome only falsifies where it carries a ground-truth bit
(test result > exit code > user correction > raw output).

**Alternatives considered**:
- Backtest prose memories against FTS5 conversation text: rejected — the index
  excludes tool outcomes by design, and lexical contradiction is unreliable.
- A perfect repo simulator (true Schema `step()`): rejected — a repo is open and
  non-deterministic; planning-inside-the-model is out of scope.

### Decision 11: Two co-equal loops (falsify + efficiency) on one outcome spine — _in flight_

**Choice**: Build the outcome index once (`outcomes.py`) and serve two loops:
**Loop 1 (falsify)** verifies skills deterministically and auto-demotes
failures; **Loop 2 (efficiency)** detects expensive roundabout tool sequences,
extracts the golden path, and promotes it — gated by Loop 1 verification.

**Rationale**: Falsification delivers *correctness* (procedures stay valid);
the golden-path loop delivers *efficiency* (don't rediscover the direct
invocation). They share the same substrate (the outcome index + step-cost
ledger) and compose: Loop 2 proposes a shortcut, Loop 1 verifies it before
promotion, so lucky one-off commands don't harden into bad shortcuts. Treating
them co-equal matches the goal of "an opencode that gets more efficient over
time," not just "tidier memory." Side benefit: indexing skill-load parts
auto-repairs the dead `.usage.json` reuse ledger (`use_count` is currently never
incremented).

**Alternatives considered**:
- Falsify-only, defer efficiency: cleaner sequencing but defers the
  token-savings payoff that motivated the feature.
- Efficiency-primary, falsify-secondary: inverts the safety layer; rejected
  because ungated shortcut promotion is the failure mode Loop 1 prevents.

### Decision 12: Deterministic falsification first; probabilistic is suggestion-only — _in flight_

**Choice**: Loop 1 evaluates `test-suite` and `declared` claims
deterministically and may auto-demote failures; correlation-based
(skill-load → subsequent outcomes) falsification is deferred and, when added,
is **suggestion-only — never auto-demote**.

**Rationale**: Deterministic checks carry ground truth, so acting on them
(auto-demote + flag) is safe. Probabilistic signals are drift-confounding and
false-positive-prone; routing them around the Ebbinghaus grace period would, on
present evidence, be net-negative. This honors the review finding that an
automated "hygiene" loop on a noisy signal makes the system worse than no-op.

**Alternatives considered**:
- Probabilistic-first (covers all skills immediately): rejected — weaker signal
  the review flagged; deterministic-first is the genuine Schema-grade path.
- Auto-demote on correlation: rejected — unsafe on a noisy, drift-blind signal.

### Decision 13: Long-horizon skill loop — evidence-driven creation + usage-driven pruning — _in flight_

**Choice**: Replace reactive-per-session skill creation with a two-part loop on
the Certified Procedures substrate. (1) A **proposer** studies many sessions,
clusters by user-request similarity (overlap coefficient + light stemming) +
the modal resolution, and **stages a proposal** when a cluster recurs across
≥ N sessions; a proposal **auto-promotes to a skill only when its common
resolution passes falsification**. (2) The reviewer's `skill create` is
**hard-gated** on a recurrence check (`proposals recurrence`) — it may not
create a new skill unless the pattern is cross-session recurrent; otherwise it
records a memory. (3) A **never-used pruner** auto-archives autolearn-created
skills with `use_count == 0` past a grace period (using the repaired use_count).

**Rationale**: The reviewer evaluated one session slice at a time and created
skills from single-conversation patterns, producing a pile of narrow, often-
unused skills with no feedback loop. The proposer adds the missing long-horizon
synthesis; the hard gate prevents new myopic creation at its source; the pruner
closes the loop on waste using the now-repaired use signal. Auto-promotion is
gated on deterministic falsification, so only verified recurring procedures
become skills.

**Alternatives considered**:
- Advisory-only proposer (reviewer free to ignore): rejected — the user flagged
  the myopic-creation waste; a soft gate would not fix it.
- Auto-archive never-used immediately: rejected — a grace period gives newly
  created skills a runway to accrue use before becoming prune-eligible.
- Human-confirm every proposal: rejected as the default — the user chose
  falsification-gated auto-promotion; manual confirm remains available via
  `proposals confirm`.

### Decision 14: Dual-shell plugin for OpenCode v1 + v2 (beta) — _shipped_
**Context**: OpenCode 2 (beta, binary `opencode2`) breaks the plugin API
intentionally: v1 function-export plugins fail v2's schema validation
(`Expected object at ["default"]`), and v2's plain-object plugins cannot be
called by v1. Both versions share `~/.config/opencode/opencode.json` and
`~/.local/share/opencode/opencode.db` (v2 sessions live in `session_v2` /
`session_message` alongside v1's `session` / `message` / `part`).

**Choice**: One shared core (`plugin/autolearn-core.mjs`) with two thin
version shells — `plugin/autolearn.js` (v1 function export, registered under
the `plugin` config key) and `plugin/autolearn-v2.js` (v2 plain-object plugin
subscribing to the server event stream, registered under `plugins`). The
v2 shell imports nothing beyond builtins and the core, so it loads without
`@opencode-ai/plugin` or node_modules. Event mapping: user text via
`session.inbox.enqueued`, assistant turns via `session.text.ended` +
`session.step.ended`, idle via `session.execution.*` boundaries. The review
wrapper selects the binary (`opencode`/`opencode2`) and v2 session cleanup
uses `opencode2 api delete`. Session search indexes both table families,
skipping v2 rows mirrored into v1 tables.

**Alternatives considered**:
- Single dual-format file: rejected — v1 requires a callable default export,
  v2's schema requires an object; the shapes are mutually exclusive.
- V2-only: rejected — v1 is the stable daily driver during beta.
- Dropping the shared config (separate config files per version): rejected —
  both versions read the same paths; the `plugin`/`plugins` key split with
  one expected v2-side warning is the least-invasive coexistence.

### Decision 15: Wiki layer — evidence compounds, skills are hypotheses; sync out of scope — _in flight_

**Choice**: Adopt the WikiSkill three-layer architecture
(docs/designs/wiki-layer/): a persistent `wiki/` notebook of diagnosed
patterns sits between raw session data and skills; a harness-written
`skill-impact.md` ledger records every skill mutation **including
rejections, kept forever**; the reviewer must read the wiki before
proposing and may make at most one skill change per review; skills carry
`PURPOSE.md` provenance. Skills remain falsify-gated and rollback-able; the
wiki is never rolled back. Cross-machine sync of wiki data is explicitly
**out of scope**: doing memory correctly requires per-device versioning and
merge semantics autolearn defers — better no sync than wrong sync.

**Rationale**: The WikiSkill ablation attributes +15 average points to the
persistent wiki — the largest single factor measured. Autolearn's
recurrence gate currently flattens diagnosed-but-not-yet-recurring patterns
into thin decaying memories; the wiki gives them a durable, compounding
home. The never-rolled-back / never-deleted asymmetry is why rejections are
kept: the next proposal builds on the failure instead of repeating it.

**Alternatives considered**:
- Keeping the memory/skill binary (status quo): loses the diagnosis; the
  discovery cost is re-paid on every recurrence.
- Syncing the wiki via the existing encrypted push/pull: rejected (Eric,
  2026-09-05) — last-write-wins merge can silently drop append-only ledger
  entries, which are exactly the data that must never be lost. Revisit with
  a git-backed or per-device-versioned design.

## Data Store Layout

### Current (shipped)

```
~/.autolearn/
├── personas/
│   └── default/               ← no --persona flag = machine default
│       ├── config.yaml            # Thresholds, intervals, flags
│       ├── memory.md              # Persistent lessons loaded into every session
│       ├── user-profile.md        # User preferences and habits
│       ├── observations.jsonl     # Event log (append-only, trimmed to 1000 lines)
│       ├── strengths.json         # Reinforcement counters per memory entry
│       ├── reviews/               # Generated review markdown files
│       │   ├── review-{timestamp}.md
│       │   └── review-exit-{timestamp}.md
│       ├── skills/                # Agent-created skills
│       │   ├── {skill-name}/
│       │   │   └── SKILL.md
│       │   ├── .archive/
│       │   └── .usage.json
│       ├── search.db              # FTS5 index over past OpenCode sessions
│       ├── wiki/                  # Wiki layer (Decision 15, docs/designs/wiki-layer/)
│       │   ├── index.md             # one-line catalog per pattern
│       │   ├── logs.md              # chronological review log
│       │   ├── skill-impact.md      # harness-written append-only ledger
│       │   ├── context.md           # composed reviewer view (generated)
│       │   ├── patterns/            # one page per diagnosed pattern
│       │   └── consolidated/        # tombstones for superseded patterns
│       ├── memories.jsonl         # Memory registry (durable, unbounded) — Memory Insight
│       ├── memory.context.md      # Generated per-session context view (loaded into sessions)
│       ├── topics.jsonl           # Topic sightings for the shift detector
│       ├── candidates.jsonl       # Rising-preference candidates pending confirmation
│       ├── bin/                   # Wrapper scripts (review-runner.sh)
│       └── .curator_state.json    # Curator run history
├── sync.yaml                  # Sync config (server URL, machine ID)
├── .encryption_salt           # Per-installation salt for key derivation
├── .persona_registry.json     # { name → uuid, sync_enabled } mapping
├── .default_persona           # Machine-wide default persona name
└── debug.log                  # Verbose plugin output (when AUTOLEARN_DEBUG=1)
```

Existing flat-layout installs are migrated to `personas/default/` automatically on first run after update (by both `autolearn.py` and `plugin/autolearn-core.mjs`).

## Feature Breakdown

| Feature | Status | Description | Components |
|---------|--------|-------------|------------|
| Conversation Monitoring | shipped | Count turns, buffer messages, detect idle, exit review (v1); turn-boundary reviews via execution events (v2) | autolearn.js / autolearn-v2.js |
| Review Spawning | shipped | Format and dispatch reviews via the version-aware wrapper (`opencode`/`opencode2`) | autolearn.js / autolearn-v2.js |
| Knowledge Store | shipped | Memory, user profile, observations, reinforcement tracking | autolearn.py |
| Skill Management | shipped | Create, patch, archive, usage tracking | autolearn.py |
| Skill Lifecycle | shipped | Auto-transition stale/archived, curator with escalation | autolearn.py |
| Review Agent | shipped | Examine conversations, extract learnings | autolearn-reviewer SKILL.md |
| Session Search | shipped | FTS5 full-text search over past OpenCode conversations | autolearn.py, search.db |
| Behavioral Escalation | shipped | Cross-project rule tracking and AGENTS.md writes | self-improving-agent/improve.py |
| E2E-Encrypted Sync | shipped | Client-side AES-256-GCM, zero-knowledge server. Crypto, CLI (sync login/push/pull/status/export-key), plugin auto-sync. rotate-key + interactive pull deferred. | `autolearn.py` (sync), `sync-server/`, `sync-convex/`, `plugin/autolearn-core.mjs` |
| Multi-Persona | shipped | Isolated knowledge stores per context (work/personal/OSS). Flat-to-personas migration is automatic and backward-compatible. | autolearn.py (persona) |
| Backend-Agnostic Sync | shipped | Fastify+SQLite (bun:sqlite) and Convex HTTP Actions both implement the same REST API. CLI is backend-agnostic. | `sync-server/`, `sync-convex/` |
| Memory Registry | shipped | Durable unbounded `memories.jsonl` replacing the 3000-char `memory.md`; absorbs `strengths.json`; lazy legacy migration. | `registry.py`, `autolearn.py` |
| Ebbinghaus Retention | shipped | Decay + strengthen-on-access scoring; tiering (hot/warm/cold/evictable); grace-period eviction. Embedding-free. | `retention.py`, `autolearn.py` |
| Context Composer | shipped | Relevance × retention ranking into a soft char budget; emits `memory.context.md`; plugin regenerates on session start + after review. | `composer.py`, `plugin/autolearn-core.mjs` |
| Recurring-Preference Detector | shipped | Cross-session SW/EMA shift detector over lexical topic signatures; rising→candidate, falling→"learned". Embedding-free. | `shift.py`, `autolearn.py` |
| Inspector UI | shipped | CLI-launchable local web app to explore registry, retention curves, candidates, skills, activity. Stdlib-only server. | `inspector_server.py`, `autolearn.py` |
| Outcome Index | in flight | Indexes `opencode.db` tool-call + step-cost + skill-load parts the FTS5 index excludes; ground-truth-weighted; repairs `.usage.json` use_count. | `outcomes.py`, `autolearn.py` |
| Procedure Falsification | in flight | Deterministic verification of skills (test-suite / declared claims); auto-demote + flag failures; probabilistic deferred as suggestion-only. | `falsify.py`, `autolearn.py` |
| Golden-Path Shortcuts | in flight | Detects expensive roundabout tool sequences, extracts the direct invocation, promotes it gated by falsification. | `shortcuts.py`, `autolearn.py` |
| Long-Horizon Proposer | in flight | Cross-session clustering of user requests + resolutions; stages proposals; auto-promotes when falsification passes. | `proposer.py`, `autolearn.py` |
| Reviewer Recurrence Gate | in flight | Hard-gates `skill create` on `proposals recurrence` — ends myopic per-session skill creation. | `autolearn-reviewer/SKILL.md`, `proposer.py` |
| Never-Used Skill Pruner | in flight | Auto-archives autolearn-created skills with `use_count==0` past a grace period (uses repaired use_count). | `autolearn.py` |
| Wiki Pattern Store | in flight | `wiki/patterns/` notebook: one page per diagnosed pattern (trigger, root cause, fix, evidence), 100-line cap, lexical index. | `wiki.py`, `autolearn.py wiki` |
| Skill-Impact Ledger | in flight | Harness-written append-only `wiki/skill-impact.md`: every create/patch/demote/archive/promote, rejections kept forever. | `wiki.py`, `autolearn.py` |
| Reviewer Wiki Protocol | in flight | Step 3.5 mandatory wiki read; pattern page on `recurrent=false`; one skill change per review. | `autolearn-reviewer/SKILL.md` |
| PURPOSE.md Provenance | in flight | Provenance doc per persona-local skill (origin, patterns addressed, evolution), backfilled for existing skills. | `wiki.py`, `autolearn.py` |
| Wiki Consolidation | in flight | Curator tombstones stale patterns superseded by skills; pages retained, never deleted. | `autolearn.py`, curator |

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Review spawning loops | `AUTOLEARN_REVIEWER=1` guard prevents recursive spawning; buffer depth check skips reviews containing review content. v2 additionally marks reviewer sessions by agent/title inside the shared service |
| Lost conversation on exit | v1: process-level `beforeExit` and signal handlers dispatch a final review before shutdown. v2: the service outlives sessions; threshold + turn-boundary reviews cover the same content |
| Memory bloat / staleness | _(Memory Insight)_ Durable unbounded `memories.jsonl` registry; forgetting driven by Ebbinghaus decay + grace-period eviction, not a FIFO char cap. Context window kept bounded by the relevance-ranked composer (soft budget). Legacy `memory.md` 3000-char FIFO cap retired post-migration. _(previously: 3000-char cap with oldest-first trimming — caused silent staleness + orphan strength records)_ |
| Stale review files | Auto-cleanup based on `stale_after_days` config |
| Secret leakage | Regex redaction of API keys, tokens, passwords from buffered messages |
| Concurrent writes | Review subprocess writes to unique timestamped files; no file-level contention |
| Sync server breach | Server stores only ciphertext — no plaintext exposure even with full DB access |
| Lost encryption key | Data is irrecoverable by design. `sync export-key` creates offline backup |
| Sync conflicts | Last-write-wins by timestamp; append-only merge for observations.jsonl |
| Persona isolation failure | Per-persona HMAC-derived keys — compromising one persona doesn't expose others |

## Related Designs

### Shipped

- [Conversation Monitoring LLD](./designs/conversation-monitoring/LLD.md) (+ 2 EARS: turn-counting, review-spawning)
- [Knowledge Store LLD](./designs/knowledge-store/LLD.md) (+ 2 EARS: memory-management, observations-logging)
- [Skill Management LLD](./designs/skill-management/LLD.md) (+ 2 EARS: skill-crud, skill-lifecycle)
- [Review Agent LLD](./designs/review-agent/LLD.md) (+ 2 EARS: conversation-evaluation, action-execution)
- [Session Search LLD](./designs/session-search/LLD.md)
- [Sync Encryption LLD](./designs/sync/encryption-LLD.md) (+ EARS)
- [Sync Protocol LLD](./designs/sync/protocol-LLD.md) (+ EARS)
- [Multi-Persona LLD](./designs/sync/persona-LLD.md) (+ EARS)

### Planned

- [Memory Insight LLD](./designs/memory-insight/LLD.md) (+ 5 EARS: registry, retention, composer, shift-detector, inspector-ui) — store/view separation, Ebbinghaus retention, recurring-preference detector, inspector UI
- [Wiki Layer LLD](./designs/wiki-layer/LLD.md) (+ [EARS](./designs/wiki-layer/wiki-layer-EARS.md)) — persistent pattern notebook, skill-impact ledger, PURPOSE.md provenance; adapted from WikiSkill (arXiv 2608.27454). Sync out of scope (Decision 15).
