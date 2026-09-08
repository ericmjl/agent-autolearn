# Wiki Layer - EARS Requirements

**Spec**: `docs/designs/wiki-layer/LLD.md`
EARS = Easy Approach to Requirements Syntax (`WHEN <trigger> THE SYSTEM SHALL <behavior>`).
Code references use `@spec WIKI-<ID>` tags.
All specs are `[ ]` (active gap) until implemented; statuses update per the house convention.

## wiki.py — pattern store core

### WIKI-PAT-001 (scaffold)
**WHEN** `wiki init` runs **THE SYSTEM SHALL** create the persona-local
`wiki/` scaffold (`wiki/index.md`, `wiki/logs.md`, `wiki/skill-impact.md`,
`wiki/patterns/`, `wiki/consolidated/`) and **SHALL** be idempotent (existing
files are never overwritten).

### WIKI-PAT-002 (page write)
**WHEN** the reviewer writes a new pattern
(`write_pattern(persona_dir, slug, title, body, session_ids=...)`) **THE
SYSTEM SHALL** create `wiki/patterns/{slug}.md` containing the pattern
statement, trigger condition, root cause, resolution, evidence session IDs,
and an `Updated` date, and **SHALL** refuse the write if the rendered page
exceeds `wiki_page_max_lines` (100).

### WIKI-PAT-003 (page update)
**WHEN** new evidence is added for an existing pattern
(`update_pattern(..., add_resolution=..., add_evidence=...)`) **THE SYSTEM
SHALL** insert the new content (not overwrite the page), refresh `Updated`,
and refuse if the page would exceed the line cap.

### WIKI-PAT-004 (index upsert)
**WHEN** a pattern page is created or updated **THE SYSTEM SHALL** keep
`wiki/index.md` containing exactly one line per pattern in the format
`- [slug](patterns/slug.md): PROBLEM + root cause + fix`, removing stale
entries for patterns moved to `consolidated/`.

### WIKI-PAT-005 (lexical retrieval)
**WHEN** `wiki read "<terms>"` runs **THE SYSTEM SHALL** lexically match the
terms against `index.md` one-liners (tokens, no embeddings) and return the
matching slugs ranked by match count.

### WIKI-PAT-006 (reviewer view)
**WHEN** `wiki compose` runs **THE SYSTEM SHALL** render `wiki/context.md`:
full `index.md`, the last `wiki_context_recent_log_lines` (40) lines of
`logs.md`, and the last 20 `skill-impact.md` entries — and **SHALL NOT**
inline any pattern page body (progressive disclosure; the reviewer opens
pages on demand).

### WIKI-PAT-007 (disabled mode)
**WHERE** `wiki_enabled: false` **THE SYSTEM SHALL** disable all wiki
commands as no-ops (empty output, exit 0) and **SHALL NOT** block skill
create/patch/archive on any wiki operation.

## Harness-written impact ledger (`wiki/skill-impact.md`)

### WIKI-IMP-001 (append on mutation)
**WHEN** `skill create`, `skill patch`, or `skill archive` succeeds **THE
SYSTEM SHALL** append one ledger line recording `date`, `action`
(create|patch|demote|archive|promote), `skill`, `summary`, `outcome`, and
`trigger` — written by `autolearn.py` code, never by the reviewer.

### WIKI-IMP-002 (rejections recorded forever)
**WHEN** a falsification verdict demotes a skill or a proposal is rejected
**THE SYSTEM SHALL** append the outcome (`demoted` / `rejected`) and the
summary of what was tried, and **SHALL NEVER** delete or rewrite existing
ledger lines (append-only), regardless of any skill rollback.

### WIKI-IMP-003 (proposer promotion recorded)
**WHEN** the long-horizon proposer promotes a proposal to a skill **THE
SYSTEM SHALL** append a `promote` entry naming the source proposal and the
motivating pattern slug(s) where known.

### WIKI-IMP-004 (reviewer must consult the ledger)
**WHEN** the reviewer prepares any skill proposal **THE SKILL PROTOCOL
REQUIRES** it to first read `wiki/skill-impact.md` (via the Step 3.5
mandatory check) and **SHALL NOT** re-propose an approach the ledger shows
was already tried and rejected; it must build on the failure or propose a
different approach.

## Reviewer protocol changes (SKILL.md)

### WIKI-REV-001 (pattern page on non-recurrence)
**WHEN** the recurrence gate answers `recurrent=false` for a diagnosed
pattern **THE REVIEWER SHALL** write or update a pattern page capturing
trigger, root cause, exact resolution, and evidence session IDs — instead
of only recording a thin memory — and **MAY** additionally record a
one-line memory pointing at the page.

### WIKI-REV-002 (dedup before create)
**WHEN** the reviewer is about to write a pattern **THE REVIEWER SHALL**
first run `wiki read "<key terms>"`; if a page for the same topic exists,
it **SHALL** update that page (evidence/roots are appended) rather than
create a near-duplicate slug.

### WIKI-REV-003 (one skill change per review)
**WHEN** a review proposes skill changes **THE REVIEWER SHALL** make at
most ONE skill mutation per review (create OR patch) — replacing the
previous allowance of up to 2 creates plus multiple patches.

### WIKI-REV-004 (single-machine boundary)
**THE REVIEWER SHALL** treat the wiki as persona-local data and **SHALL
NOT** invoke any sync command for wiki files (sync is out of scope per LLD
Design Decision 5).

## PURPOSE.md provenance

### WIKI-SKL-001 (create-time provenance)
**WHEN** a skill is created via `skill create` with motivating pattern
slug(s) **THE SYSTEM SHALL** write `skills/{name}/PURPOSE.md` documenting
Origin, Patterns Addressed (with links to `wiki/patterns/{slug}.md`), and
Evolution History.

### WIKI-SKL-002 (patch-time provenance)
**WHEN** `skill patch` is applied to a persona-local skill **THE SYSTEM
SHALL** append a dated line to that skill's `PURPOSE.md` (creating it if
absent) summarizing the change.

### WIKI-SKL-003 (backfill)
**WHEN** `wiki init` runs on an existing persona, or the user runs `wiki
backfill-purposes` **THE SYSTEM SHALL** create `PURPOSE.md` for every
autolearn-created persona-local skill missing one, sourcing origin from
proposals.json `promoted_skill` records, else `skill-impact.md` `create`
entries, else the placeholder "origin: created before the wiki layer;
provenance unknown" — and **SHALL NOT** modify hand-authored skills under
`~/.agents/skills/`.

## Curator / retention integration

### WIKI-CUR-001 (consolidation pass)
**WHEN** the curator runs **THE SYSTEM SHALL** inspect `patterns/*.md`
`Updated` dates and, for a pattern untouched longer than
`stale_after_days` (30) that is superseded by a skill, write a one-line
tombstone into `wiki/consolidated/`, remove its index entry, and keep the
pattern page (never delete; the wiki is append-only history).

### WIKI-CUR-002 (no Ebbinghaus eviction of patterns)
**WHILE** the wiki-layer feature is versioned v1 **THE SYSTEM SHALL NOT**
evict pattern pages via the memory Ebbinghaus mechanism (eviction applies
to memories only); consolidation in WIKI-CUR-001 is the only structural
retirement path.

## Inspector

### WIKI-INS-001 (wiki overview API)
**WHEN** the inspector serves `/api/wiki` **THE SYSTEM SHALL** return the
pattern list (slug, title, updated date) and the last 20 ledger entries.

## Related Documents

- [Wiki Layer LLD](./LLD.md)