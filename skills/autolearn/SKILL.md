---
name: autolearn
description: |
  Self-improvement system for coding agents: one skill, three modes, one
  shared signal taxonomy. MODES — (1) OBSERVER (always-on, in-session):
  after EVERY user message, silently check the signal taxonomy below; if the
  user corrected you, stated a preference or workflow spec, or expressed
  frustration about repetition, record it with
  `uv run ~/.agents/skills/autolearn/scripts/improve.py observe "<rule>" --project <name>`.
  Do NOT load the full skill for observation — this description is
  sufficient; load references/observer.md only for escalation thresholds or
  writing to AGENTS.md. (2) REVIEWER (autonomous subagent, spawned by the
  autolearn plugin every N user messages): read references/reviewer.md and
  follow its action protocol — evaluate the conversation, check the wiki and
  past sessions, record memories, update the user profile, create or patch
  at most ONE skill. Do NOT load this mode during normal conversation.
  (3) CURATOR (scheduled job): read references/curator.md — consolidate
  narrow skills into umbrellas, archive stale skills, maintain the library.
  Command reference for both CLIs (autolearn.py, improve.py) lives in
  references/cli.md.
license: MIT
verify:
  command: "uv run --with pytest --with python-slugify pytest scripts/ --ignore=scripts/test_sync_e2e.py --ignore=scripts/test_sync_crypto.py"
  expect_exit: 0
---

# Autolearn

You are part of a self-improvement system. A harness plugin (OpenCode v1/v2,
pi) buffers each conversation and spawns a REVIEWER subprocess every N user
messages (default 5), on idle, and at session exit. You may also be acting
as the in-session OBSERVER or the scheduled CURATOR. Your mode determines
which reference to load:

| Mode | Who loads it | Reference |
|------|--------------|-----------|
| Observer | The main agent, every session (trigger lives in the description) | `~/.agents/skills/autolearn/references/observer.md` |
| Reviewer | The `autolearn-reviewer` subagent spawned by the plugin | `~/.agents/skills/autolearn/references/reviewer.md` |
| Curator | A scheduled maintenance job (weekly by default) | `~/.agents/skills/autolearn/references/curator.md` |

Read your mode's reference file with the `read` tool before acting. The
signal taxonomy below is shared by every mode and is the single source of
truth; the references add mode-specific protocol. CLI usage for both
scripts is catalogued in `references/cli.md`.

## Signal Taxonomy (all modes)

### Strong signals (always act)

1. **User corrections**: "don't do X", "use Y instead", "that's wrong",
   "not like that", "I said Z"
2. **Explicit preferences**: "I prefer X", "always do Y", "from now on, Z",
   "never do X again"
3. **Declarative workflow specifications**: the user describes how a
   recurring task should work — even when no mistake was made. These are
   prospective specs, not corrections. Examples:
   - "they should be one post one week" (cadence spec)
   - "LinkedIn should follow Bluesky schedule" (cross-channel sync rule)
   - "we don't use global pip or pip3 anywhere here" (system-wide tool rule)
   - "I want tests run before each commit" (workflow ordering)
   - "use PEP 723 inline script metadata for any Python scripts" (convention)
   These often use "should", "we use", "we don't", "I want", "needs to be"
   without explicit "I prefer" or "always" markers.
4. **Frustration about repetition**: "again?", "I keep telling you",
   "every time", "I've said this before"
5. **Explicit instruction to remember**: "remember this", "write that down",
   "note this for next time"
6. **Workarounds that worked**: non-obvious techniques, debugging paths,
   fixes that resolved an issue
7. **Failure diagnoses with root cause (conditionalized negatives)**: capture
   a failure ONLY when you can state all three: (a) the TRIGGER CONDITION
   under which it fails, (b) the ROOT-CAUSE reason, and (c) the FIX or
   WORKAROUND. With all three it is a guardrail with an escape hatch —
   record it (skill if repeatable, memory if one-off). Missing any of the
   three → skip: a bare "X is broken" hardens into an over-generalized
   refusal and makes the agent wrong.
8. **Token-efficiency mandate (proactive)**: the purpose of recording is to
   make FUTURE interactions less token-heavy. Scan for EXPENSIVE DISCOVERY
   that was performed but not distilled: `--help` chains 2+ levels deep,
   trial-and-error API probing, re-reading the same docs across sessions,
   multi-step diagnostics that converged on a fix. When you see one, record
   a skill (repeatable procedure) or memory (one-off fact) so the next
   session reaches the answer directly. Threshold for skills: >=2 probing
   steps AND the tool recurs across sessions.

### Moderate signals (act if seen more than once)

1. **Tool choice patterns**: user consistently prefers one tool over another
2. **Code style preferences**: naming, formatting, structure choices
3. **Workflow patterns**: how the user approaches tasks, ordering preferences
4. **Skill gaps**: moments where the agent struggled or didn't know something

### Weak signals (record but don't create skills)

1. **Contextual facts**: project-specific information worth remembering
2. **Environment details**: tool versions, config quirks, platform specifics

### What NOT to capture

- One-time task instructions ("add a button", "rename this variable")
- Clarification questions
- Normal conversational flow
- Environment-dependent failures (missing binaries, network issues)
- Session-specific transient errors
- **Bare** negative claims about tools ("X is broken", "don't use X") —
  unless you can conditionalize them per strong signal #7

### Generalization rule

When the user states a rule with system-wide or project-wide scope
("anywhere", "on my system", "always", "every project", "we don't use"),
record the GENERAL rule, not the specific instance that triggered it.

Bad: "eric-video skill scripts should use uv run" (too narrow)
Good: "Never use global pip or pip3. Use PEP 723 inline script metadata +
`uv run` for all Python scripts."

If a narrow version of a rule already exists and the user re-states it more
broadly, replace the narrow entry with the general one.

## Hard Gates (all modes)

- **One skill change per review**: at most ONE skill mutation (create OR
  patch) per review cycle. Everything else waits for the next cycle.
- **Recurrence gate for creation**: never `skill create` without verifying
  the pattern recurs across sessions (`proposals recurrence`); route
  single-session patterns to the wiki instead.
- **Never delete skills** — archive only; archives are recoverable.
- **Never touch user-installed skills** — only skills autolearn created.
- **Never write secrets** to memory, profiles, or skills.
- **Prefer strengthen over add** for semantic duplicates in the registry.

## Data Layout

All state lives under `~/.autolearn/` (override with `AUTOLEARN_HOME`):

```text
~/.autolearn/
├── personas/default/
│   ├── memory.context.md    # composed context view (injected every session)
│   ├── memories.jsonl       # durable lesson registry (Ebbinghaus decay)
│   ├── user-profile registry entries (type="user")
│   ├── observations.jsonl   # append-only audit log
│   ├── reviews/             # conversation slices handed to reviewers
│   ├── wiki/                # pattern notebook + skill-impact ledger
│   ├── skills/              # agent-created skills
│   └── config.yaml          # thresholds (review_threshold: 5, ...)
└── sync.yaml                # optional cross-machine sync
```

Escalation rules (behavioral rules promoted into AGENTS.md files) live in
`~/.agent-improvement/rules.yaml`, managed by `improve.py` — see
`references/observer.md`.
