# CLI Reference

Two scripts power every mode. Paths assume the standard install; both honor
`AUTOLEARN_HOME` for the store root.

```bash
AL="$HOME/.agents/skills/autolearn/scripts"
uv run $AL/autolearn.py <command>     # store, memory, wiki, skills, sync
uv run $AL/improve.py <command>       # behavioral rules + AGENTS.md escalation
```

## autolearn.py — store & memory

| Command | Purpose |
|---------|---------|
| `init` | Initialize the autolearn store |
| `memory add "<lesson>"` | Add a durable lesson to the registry |
| `memory list` | List entries (check for semantic duplicates first) |
| `memory strengthen "<keyword>"` | Reinforce a semantically-duplicate entry |
| `memory weaken "<keyword>"` | Reduce reinforcement on an entry |
| `memory remove "<keyword>"` | Remove entries matching keyword |
| `memory strengths` | Show reinforcement statistics |
| `memory compose` | Regenerate `memory.context.md` from the registry |
| `user add "<preference>"` | Add a user-profile preference (`type="user"`) |
| `user list` / `user remove "<keyword>"` | List / remove profile entries |
| `retention score` | Recompute Ebbinghaus retention scores & tiers |
| `retention evict` | Evict memories past the cold grace period |
| `persona create/list/switch/archive/rename` | Manage knowledge personas |

## autolearn.py — skills & wiki

| Command | Purpose |
|---------|---------|
| `skill create <name> "<desc>" --patterns "<slugs>"` | Create a skill (recurrence gate REQUIRED — reviewer.md Step 7) |
| `skill patch <name> "<section>" "<content>"` | Patch an existing skill (no gate) |
| `skill archive <name>` | Archive a skill (never delete) |
| `skill list` / `skill usage` | List skills / show usage telemetry |
| `curator run` / `curator status` | Auto stale/archive transitions / state |
| `wiki compose` | Render `wiki/context.md` (reviewer pre-flight, MANDATORY) |
| `wiki read "<terms>"` | Lexically search the pattern index |
| `wiki show <slug>` | Print one pattern page |
| `wiki list` / `wiki init` / `wiki backfill-purposes` | Index / scaffold / backfill PURPOSE.md |
| `logs append "<summary>"` | Append a review summary to wiki/logs.md |
| `impact append` | Skill-impact ledger entry (harness/debug) |

## autolearn.py — search, logging, proposals, extras

| Command | Purpose |
|---------|---------|
| `search init` / `search query "<terms>"` / `search sessions "<terms>"` / `search status` | FTS5 search over past sessions |
| `log review-complete --observations N [--memory-updated] [--user-profile-updated] [--skills-created N] [--skills-patched N] --topics "<t1,t2>"` | Log review outcome |
| `log review-complete --nothing` | Log an empty-handed review |
| `proposals recurrence "<terms>"` | Recurrence gate for skill creation |
| `proposals scan` | Long-horizon scan → verify → promote (scheduled tick) |
| `topics scan` / `topics candidates` | Recurring-preference (shift) detector |
| `outcomes init` / `outcomes status` | Tool-call outcome index (Certified Procedures spine) |
| `falsify run` / `falsify verdicts` | Verify skills against their claims |
| `sync login/logout/export-key/push/pull/status` | Cross-machine E2E-encrypted sync |
| `ui` | Launch the inspector UI |

## improve.py — behavioral rules

| Command | When | Purpose |
|---------|------|---------|
| `observe "<rule>" [--project P] [--domain D] [--context C] [--explicit-scope global\|local]` | After a correction/preference (observer Tier 1) | Record an observation |
| `status` | Session start | Show all rules and counts |
| `due` | Session end or `/improve` | Show pending escalations |
| `escalate --dry-run` | Escalation due | Preview what would be written where |
| `escalate --apply` | Escalation due | Write rules to the right AGENTS.md + confirm in one step |
| `confirm <id> --scope global\|local` | After a manual AGENTS.md write | Mark rule as written |
| `history <id>` | Debugging | Full observation history |
| `domains` | Reference | List the domain taxonomy |
| `stale [--days N]` | Periodic check | Rules not reinforced in N days (default 90) |
| `seed --from-agents-md` | First-time setup | Import rules from existing AGENTS.md |
| `init [--force]` | First-time setup | Initialize rules.yaml |

See `references/observer.md` for the escalation decision tree and AGENTS.md
writing protocol.
