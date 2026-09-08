# Wiki Layer - Low-Level Design

**Created**: 2026-08-30
**Revised**: 2026-08-30 (page cap 100 lines; PURPOSE.md backfill; sync removed from scope)
**HLD Link**: ../../high-level-design.md

## Overview

Adds a persistent, never-rolled-back knowledge layer (`wiki/`) between raw
experience and skills, completing the three-layer architecture from
WikiSkill (arXiv 2608.27454, Google Research, Aug 2026): immutable raw
traces -> compounding wiki -> gated skills.

Today autolearn's reviewer is binary: a finding either becomes a memory
(one decaying line) or a skill (gated on cross-session recurrence via
`proposals recurrence`). Diagnosed patterns that are real but not yet
recurrent get flattened into thin memories or dropped — and the discovery
cost is re-paid on every recurrence. The wiki is the missing home for
*diagnosed patterns accumulating evidence*.

The paper's ablation backs the priority: removing the wiki dropped average
benchmark accuracy from 63.7 to 48.7 (-15.0); no other single component
compared close. This LLD adopts its four load-bearing mechanisms at
autolearn's scale.

## Context — what the paper does, what we adopt

| WikiSkill mechanism | Adoption |
|---------------------|----------|
| `wiki/patterns/` (one page per failure/success pattern, root cause + exact commands + fix, patch-edited incrementally) | Adopt as-is (memory-vs-skill middle layer) |
| `wiki/logs.md` (per-iteration evolution log) | Adopt (per-review) |
| `wiki/skill-impact.md` — programmatic ledger of every skill change + outcome, including rejected diffs | Adopt (written by `autolearn.py`, never by the reviewer) |
| Proposer must read ledger first; never re-propose rejected approaches | Adopt (reviewer SKILL.md step) |
| Wiki never rolled back, even when skills roll back | Adopt (core asymmetry: skills are hypotheses, wiki is evidence) |
| One atomic skill proposal per iteration | Adopt (one skill change per review) |
| `PURPOSE.md` provenance per skill | Adopt |
| Inference agent must NOT see raw wiki during rollouts | Adopt as "pointer, not copy" (see Access Boundaries) |
| Fresh wiki per run, ~8 iterations, no pruning (paper's stated limitation) | Not adopted — we run continuously, so Ebbinghaus decay governs the wiki too |

## Architecture

```
opencode sessions (raw/)
  search.db + outcomes.db + reviews/
        │
        ▼
autolearn-reviewer (per review cycle)
  1. READ wiki/context.md  (composed view: index + recent log + ledger tail)
  2. find/patch pattern pages (dedup diagnosis knowledge)
  3. recurring?  ──yes──> ONE atomic skill create/patch (falsify-gated)
       │no                    │
       ▼                      ▼
  write pattern page     autolearn.py appends BOTH outcomes to
                         wiki/skill-impact.md (harness-written ledger)
        │
        ▼
retention score (existing) ── aged patterns ──> consolidated/archived
```

## Data Models

### Persona directory additions (`~/.autolearn/personas/{name}/wiki/`)

```
wiki/
├── index.md           # catalog: one line per pattern
├── logs.md            # chronological log: review date, findings, actions
├── skill-impact.md    # harness-written ledger (see schema below)
├── patterns/
│   └── {slug}.md      # one page per diagnosed pattern
└── consolidated/      # retirement home for decayed patterns (optional)
```

### Pattern page (`wiki/patterns/{slug}.md`)

Slugified from the review's topic list; hard cap 100 lines
(`wiki_page_max_lines`). The paper kept pages to 10-30 lines; we widen the
ceiling so a full diagnosis (trigger, cause, resolution variants, evidence)
fits without consolidation churn — the cap stays as a growth guard, not a
compression target.

| Field (embedded as markdown) | Description |
|------------------------------|-------------|
| Pattern statement | One-sentence title: problem + root cause + fix |
| Trigger condition | When this bites (reviewer signal #6a rule applies) |
| Root cause | WHY, not just WHAT |
| Resolution | Exact command / procedure, copied verbatim from evidence |
| Evidence | Session IDs + dates (pointers into search.db sessions) |
| Updated | Last-touched date (feeds retention/consolidation) |

### `wiki/index.md`

One bullet per pattern, fixed format (paper's "Index Description Quality"
rule — the index line must let the reader judge relevance without opening
the page):

```markdown
- [slug](patterns/slug.md): PROBLEM + root cause + fix (one or two lines)
```

### `wiki/skill-impact.md`

Append-only; every line written by `autolearn.py` code, not by the agent.
One entry per skill mutation or verification verdict:

| Field | Type | Description |
|-------|------|-------------|
| date | string | ISO datetime of the mutation |
| action | enum | `create` \| `patch` \| `demote` \| `archive` \| `promote` |
| skill | string | Target skill name |
| summary | string | What changed (diff summary or verdict text) |
| outcome | enum | `accepted` \| `rejected` \| `demoted` \| `archived` |
| trigger | enum | `review` \| `falsify` \| `proposer` \| `curator` |

Rejected entries stay forever (they are the "do not re-propose" memory).
The reviewer is REQUIRED to read this ledger before proposing any skill
change.

## Component APIs

`wiki.py` (new sibling of `composer.py`, `retention.py`; stdlib only):

```python
def pattern_path(persona_dir, slug) -> Path
def write_pattern(persona_dir, slug, title, body, *, session_ids) -> Path
    # creates patterns/{slug}.md; caller supplies the page body
def update_pattern(persona_dir, slug, *, add_resolution=None,
                   add_evidence=None) -> Path
    # insert-after semantics for new evidence; refreshes `Updated`
def ensure_index(persona_dir) -> Path            # create index.md if absent
def upsert_index_entry(persona_dir, slug, one_liner) -> None
def append_log(persona_dir, *, summary, counts) -> None
def append_skill_impact(persona_dir, *, action, skill, summary,
                        outcome, trigger) -> None
def list_patterns(persona_dir) -> list[dict]     # slug, title, updated
def find_relevant_patterns(persona_dir, tokens) -> list[Path]
    # lexical match over index.md one-liners (same approach as composer)
def retention_dates(persona_dir) -> dict        # slug -> last_updated
```

### CLI commands (added to `autolearn.py`)

| Command | Description |
|---------|-------------|
| `wiki init` | Create `wiki/` scaffold (idempotent) |
| `wiki list` | List patterns + last-updated dates |
| `wiki show <slug>` | Print one pattern page |
| `wiki read <terms>` | Lexical search over index one-liners |
| `wiki compose` | Render `wiki/context.md` (reviewer-facing view) |
| `logs append "<summary>"` | Reviewer appends the evolution log |
| `impact append --action ... --skill ... --outcome ... ...` | Not a reviewer command — used internally by `skill create/patch/archive` and falsify |

### Reviewer-facing view (`wiki/context.md`)

`memory compose` (runtime view) is unchanged. `wiki compose` renders the
reviewer's working set: full `index.md` + tail of `logs.md` (default 40
lines) + last 20 `skill-impact.md` entries. The reviewer reads pages on
demand from there — progressive disclosure, no unbounded inline.

## Config additions (`config.yaml`)

```yaml
wiki_enabled: true
wiki_page_max_lines: 100
wiki_context_recent_log_lines: 40
```

## Integrations

### 1. `autolearn.py` (harness writes the ledger)

- `cmd_skill_create`, `cmd_skill_patch`, `cmd_skill_archive`: after the
  mutation succeeds, call `append_skill_impact(...)`. The reviewer never
  edits the ledger (paper rule: programmatic, ground-truth).
- `falsify.py` auto-demote path: append a `demote` entry with outcome
  `demoted`, trigger `falsify`.
- `proposer.py` promotion: append a `promote` entry.
- On any skill-create/patch that carries a `purpose_patterns` field,
  link it in the entry summary.

### 2. Reviewer SKILL.md changes (prompt-level, no code)

- Step 3.5 (new): run `wiki read "<key terms>"` + read the last entries
  of `wiki/skill-impact.md`. If a pattern page exists for this topic,
  UPDATE it (Step 4) instead of starting fresh. If the ledger shows the
  exact approach was already tried and rejected, DO NOT re-propose it;
  build on it or propose something different.
- Step 7 (revision): when the recurrence gate answers `recurrent=false`,
  the reviewer now **writes a pattern page** (`via wiki.py` helpers)
  instead of only recording a thin memory. The memory may additionally
  carry a one-line pointer to the page.
- Step 7 (new restriction): at most ONE skill change per review
  (create OR patch); every created skill gets a `PURPOSE.md` naming the
  motivating pattern slug(s), and every patch appends a dated line to it.

### 3. Reviewer agent prompt (opencode.json)

The reviewer agent's context budget now covers `wiki/context.md` via its
own reads — no `instructions` entry changes; the pointer lives in the
reviewer skill only. Runtime sessions never receive wiki content.

### 4. Retention / curator extension

`retention score` already sweeps persona files? No — it sweeps the memory
registry only. New behavior: the curator (weekly) also inspects
`patterns/*.md` last-updated dates:

- Pattern untouched > `stale_after_days` (existing config, 30) AND
  superseded by a skill → move a one-line tombstone into
  `wiki/consolidated/` and keep the page (never delete; the wiki is
  append-only history).
- Patterns are never auto-evicted by Ebbinghaus in v1; eviction applies to
  *memories*. This is deliberate (paper: the wiki is the persistent
  substrate), but the consolidated/ path gives the curator a pruning lever
  for runaway growth. Revisit after 6 months of real use.

### 5. PURPOSE.md provenance — backfill for ALL existing skills

`PURPOSE.md` is not just for new skills: existing autolearn-created skills
are backfilled, and every patch keeps it current going forward.

- Backfill trigger: first `wiki init` on an existing persona, plus a
  manual `wiki backfill-purposes` command.
- Provenance sources, in order: `proposals.json` (`promoted_skill` →
  promotion record), then `skill-impact.md` `create` entries, else a
  placeholder: "origin: created before the wiki layer; provenance unknown."
- Going forward: every `skill patch` appends a dated evolution line;
  every `create` files the initial document.
- Scope: persona-local `skills/{name}/` only. Hand-authored skills in
  `~/.agents/skills/` are outside autolearn's write boundary (never
  modified — they belong to their own repos/sync flows).

### 6. Inspector UI

`/api/wiki` — pattern list with updated dates + recent ledger entries, in
the overview page.

## Access Boundaries ("pointer, not copy")

The paper's §5.1 second ablation: giving the inference agent wiki access
*during evolution degraded skill quality* (63.7 -> 60.9) because the agent
satisfies its needs from the wiki instead of the skills, making trajectories
less informative. Autolearn equivalent: runtime sessions get
`memory.context.md` only, which may contain a one-line pointer to
`wiki/index.md` for curious agents; the reviewer (the learning-loop
component) reads the wiki freely. We do NOT add wiki content to any
instructions-loaded file.

## Error Handling

| Condition | Behaviour |
|-----------|-----------|
| `wiki/` missing (pre-feature persona) | All commands no-op/empty; `skill create` still appends after `wiki init`-equivalent auto-create. Never fatal. |
| `wiki_enabled: false` | Ledger + wiki commands disabled; skill CRUD unaffected. |
| Duplicate pattern slug | `update_pattern` path; if title genuinely differs, reviewer picks a more specific slug (lexical dedup same as memory strengthen). |
| Page exceeds `wiki_page_max_lines` | `write_pattern` refuses with an actionable error; reviewer consolidates the page before appending. |
| Two machine-side writes collide | Out of scope: sync is removed from autolearn's scope (see Design Decisions #5). The wiki is documented as single-machine, single-writer data. |
| Reviewer forgets to read ledger | SKILL.md makes it a mandatory numbered step; the ledger is also printed in `wiki compose` output the reviewer loads first. |

## Edge Cases

1. **Pattern proves out (recurs)** — the skill is written *from* the
   pattern page; the page stays (it is history, not a duplicate).
   `skill-impact.md` gains a `create` entry referencing the pattern slug.
2. **Patch rejected by falsification** — skill rolls back (existing
   behavior), but the ledger records outcome `rejected` and the reviewer's
   next cycle starts from that fact. This is the paper's case study
   (Iteration 0 rejected -> Iteration 1 accepted builds on the failure).
3. **Two machines review in parallel** — pattern files are last-write-wins
   (acceptable: page edits are additive prose); the ledger merges as a
   dated union so no verdict is lost.
4. **Reviewer wants to create a skill but ledger shows an existing,
   un-recalled patch covering it** — it patches instead of duplicating;
   the ledger line for the patch links back to the same pattern.
5. **Very old pattern with no skill** — curated into `consolidated/`
   tombstone by the curator flow; still discoverable via `wiki read`.

## Non-Goals

- **No sync integration.** Sync is out of wiki-layer scope (Decision 5
  below). The wiki is documented as single-machine data.
- No vector embeddings for pattern retrieval (lexical index-line match,
  consistent with Decision 9).
- No auto-eviction of patterns (see Integrations #4 for the curator lever
  and the deferred decision).
- No automatic wiki->skill promotion beyond what the falsify-gated
  long-horizon proposer already stages (this design changes *where the
  reviewer gets its evidence*, not who promotes).

## Design Decisions (paper deltas)

1. **Ebbinghaus for the wiki's long horizon, not endless growth.** The
   paper names the missing pruner as its main limitation; we already ship
   the decay machinery, so the wiki gets consolidation while keeping the
   never-delete rule inside a review horizon.
2. **Harness-written ledger**, adopted literally from the paper: agent-
   editable audit trails stop being ground truth.
3. **Pointer-not-copy access boundary** instead of the paper's hard
   injection control: our runtime injects files via `instructions`, and we
   simply never point them at the wiki.
4. **PURPOSE.md for autolearn-created skills, backfilled** — every
   existing persona-local skill gets provenance (or an explicit
   "provenance unknown" placeholder) at `wiki init`; hand-authored skills
   in `~/.agents/skills/` remain outside the write boundary.
5. **Single-machine knowledge; sync removed from scope** (Eric,
   2026-08-30): doing memory *correctly* needs per-device versioning and
   merge semantics that autolearn deliberately defers — better to have no
   sync than wrong sync. Consequences: no sync code changes in this
   design; the wiki directory is treated as single-writer;
   documentation states the boundary. Revisit only if a future design
   brings git-backed or per-device-versioned stores.

## Dependencies

- `wiki.py`: new module, Python >=3.11 stdlib only (mirrors composer.py).
- No new third-party deps; no plugin changes; no sync changes
  (sync is out of scope per Design Decision 5).
- Paper: arXiv 2608.27454v1 (WikiSkill, Tang et al., Google Research).
  Method = §3 + Algorithm 1; ablations = §5.1 Tables 3; prompts = App. E.

## Build Order

1. `wiki.py` + `autolearn.py` wiki commands + tests (init/list/show/read/
   compose; pattern write/update; index upsert).
2. Harness ledger writes in `cmd_skill_create/patch/archive` + falsify
   demote + proposer promote + tests.
3. Reviewer SKILL.md steps (3.5 ledger read; 7 pattern-write on
   `recurrent=false`; one-change-per-review; PURPOSE.md create + patch
   updates).
4. PURPOSE.md backfill for existing persona-local skills
   (`wiki init` + `wiki backfill-purposes`).
5. Curator consolidation pass + inspector `/api/wiki`.
6. HLD: Decision 15 + feature rows + Related Designs link.

## Related Documents

- [High-Level Design](../../high-level-design.md)
- [Wiki Layer EARS](./wiki-layer-EARS.md)
- [Long-Horizon Skills LLD](../long-horizon-skills/LLD.md) (staging +
  falsify-gated promotion — the wiki feeds this reviewer/proposer pair)
- [Certified Procedures LLD](../certified-procedures/LLD.md)
  (falsification gate this design's accept/reject trails record into)
- [Memory Insight LLD](../memory-insight/LLD.md) (store/view separation the
  wiki's runtime boundary mirrors)
- WikiSkill paper: <https://arxiv.org/abs/2608.27454> (three-layer
  architecture, Algorithm 1, §5.1 ablations, Appendix E prompts)