# Observer Mode

You are the always-on, in-session observer. The trigger for this mode lives
in the skill description: after every user message, silently check the
signal taxonomy (SKILL.md). If a strong signal fired, record it with ONE
command and move on with the user's task — observation never interrupts the
work. Load this reference only when you need the escalation protocol, the
domain taxonomy, or the AGENTS.md writing rules.

CLI: `$HOME/.agents/skills/autolearn/scripts/improve.py`
Data: `~/.agent-improvement/rules.yaml` (override with `AGENT_IMPROVEMENT_HOME`)

## Two-Tier Activation

### Tier 1: Observation (always in context, via the description)

1. After every user message, silently evaluate: did the user correct me,
   state a preference or workflow spec, or show frustration about
   repetition?
2. If yes, run one CLI command to record it.
3. Do NOT load this file for observation alone.

```bash
uv run $HOME/.agents/skills/autolearn/scripts/improve.py observe "<rule>" \
  --project <name> [--domain <domain>] [--context "<what happened>"] \
  [--explicit-scope global|local]
```

**Rule phrasing**: imperatives. Bad: "user doesn't like pip". Good: "Use uv
tool for Python CLI tools, never pip3 install".

**Explicit scope**: `--explicit-scope global` when the user says "always",
"everywhere", "in all projects". `--explicit-scope local` for "in this
project", "just here".

**Project detection**: if `--project` is omitted, the CLI auto-detects from
`git remote get-url origin`.

### Tier 2: Escalation & writing (this file, loaded on demand)

Load when checking escalation thresholds, writing rules into AGENTS.md
files, reviewing the domain taxonomy, or running `/improve`.

## Session Startup Protocol

At the start of each session:

```bash
uv run $HOME/.agents/skills/autolearn/scripts/improve.py status
```

This loads all active rules into context. Rules with `written_to` entries
are confirmed — treat them as hard rules. Rules without are tentative —
apply them but don't enforce rigidly.

## Domain Taxonomy

Domains determine whether a rule escalates to global or stays
project-local.

### General domains (escalate to global when cross-project)

| Domain | Examples |
|--------|----------|
| `python-tooling` | Package managers, virtualenvs, linting |
| `git-practices` | Commit messages, branching, PR conventions |
| `security` | Input validation, secret handling, permissions |
| `code-style` | Naming, formatting, comment style |
| `error-handling` | Try/catch patterns, error messages |
| `testing` | Test frameworks, coverage, test structure |
| `documentation` | Docstrings, README style, API docs |
| `communication` | Response length, tone, formatting |
| `tool-usage` | Which tools to use, search patterns |
| `search-patterns` | Grep vs glob, code navigation |

### Project-specific domains (stay local even when cross-project)

| Domain | Examples |
|--------|----------|
| `auth-architecture` | Auth middleware, JWT patterns, session handling |
| `import-patterns` | Module structure, barrel exports, aliases |
| `file-structure` | Directory layout, naming conventions |
| `api-design` | REST patterns, endpoint naming, response format |
| `database-queries` | ORM patterns, migration style, query conventions |
| `ui-components` | Component patterns, state management, styling |
| `config-management` | Environment variables, config files, secrets |
| `deployment` | CI/CD, Docker, infrastructure |

If a rule's domain is `unknown`, treat it as project-specific until the
domain is identified.

## Escalation Decision Tree

After recording an observation, the CLI prints the recommended action:

```text
IF explicit_scope == "global":
    → Write to global AGENTS.md immediately (any count)

ELSIF explicit_scope == "local":
    → Never escalate beyond project AGENTS.md (any count)
    → Write to project AGENTS.md at count >= 2

ELSIF total_count == 1:
    → Apply in-session only, no file write

ELSIF total_count >= 2 AND cross_project == false:
    → Write to project AGENTS.md

ELSIF total_count >= 2 AND cross_project == true AND domain is general:
    → Write to global AGENTS.md

ELSIF total_count >= 2 AND cross_project == true AND domain is project-specific:
    → Write to each affected project's local AGENTS.md

ELSIF domain == "unknown":
    → Treat as project-specific, do not escalate until domain is identified
```

### Checking for due escalations

At session end (or when the user runs `/improve`):

```bash
uv run $HOME/.agents/skills/autolearn/scripts/improve.py due
```

### Escalating

```bash
uv run $HOME/.agents/skills/autolearn/scripts/improve.py escalate --dry-run  # preview
uv run $HOME/.agents/skills/autolearn/scripts/improve.py escalate --apply    # write + confirm
```

`--apply` patches each AGENTS.md file (finding or creating the right
`## Section` based on domain), skips duplicates, records `written_to`, and
marks escalation in one step.

## Writing Rules into AGENTS.md

| Scope | Path |
|-------|------|
| Global | `~/.config/opencode/AGENTS.md` (or the harness equivalent) |
| Local | `<project-root>/AGENTS.md` |

1. Read the existing AGENTS.md file
2. Find the appropriate section (or create one)
3. Add the rule as a concise bullet — use the exact rule text from rules.yaml
4. Do NOT duplicate rules that already exist in the file
5. Confirm:

```bash
uv run $HOME/.agents/skills/autolearn/scripts/improve.py confirm <rule-id> --scope global|local
```

Format rules as concise, actionable instructions:

```markdown
## Python Tool Management

- Use `uv tool` for installing Python CLI tools (never `pip3 install`).
- Use `uv run -- python -c "..."` for one-off Python commands.
```

Not like:

```markdown
- The user has corrected me 3 times about pip. I should use uv instead.
```

Always run `confirm` after writing — otherwise the system thinks the rule
is still pending and will try to escalate it again.

## Anti-Patterns

- **Over-recording**: "try the other approach" is exploration, not a rule.
  Record only when the intent is clearly "do it this way from now on."
- **Premature escalation**: a rule said once is NOT a rule. Wait for
  repetition (count >= 2) unless the user explicitly says "always" or
  "everywhere."
- **Vague rules**: "I prefer clean code" is not actionable. Translate to
  specific behaviors.
- **Conflicting rules**: record the new observation, check which rule has
  more evidence (count, recency); if both are strong, ask the user; retire
  the loser with `"retired": true` in rules.yaml.
- **Writing without confirming**: always `confirm` after a write.

## How the Observer Relates to the Reviewer

The reviewer mode (spawned every N user messages by the plugin) ALSO calls
`improve.py observe` (reviewer.md Step 4) — the observer catches signals
immediately in-session; the reviewer catches what the observer missed and
does the heavier wiki/memory/skill work. Same taxonomy, same rule store,
different latency.
