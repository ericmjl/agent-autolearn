# agent-autolearn

Self-improvement engine for coding agents. One enforced methodology, multiple harnesses: works with **OpenCode v1 (`opencode`)**, **OpenCode v2 beta (`opencode2`)**, and **pi**, installed side by side. Learns from your conversations, captures corrections and preferences, and escalates behavioral rules so your coding agent improves over time.

<p align="center">
  <a href="https://www.linkedin.com/feed/update/urn:li:activity:7481434645415477248/">
    <img src="assets/junpeng-lao-quote.svg" alt="Junpeng Lao: Really like opencode-autolearn, it's the worklog/self-improvement loop done properly." width="720">
  </a>
</p>

## Supported harnesses

| Harness | Adapter | Installed to | Memory injection | Review subprocess |
|---------|---------|--------------|------------------|-------------------|
| OpenCode v1 | `plugin/autolearn.js` | `~/.config/opencode/plugins/` | `instructions` entry in `opencode.json` | `opencode run --agent autolearn-reviewer` |
| OpenCode v2 (beta) | `plugin/autolearn-v2.js` | `~/.config/opencode/plugins/` | `instructions` entry in `opencode.json` | `opencode2 run` + HTTP session delete |
| pi | `plugin/autolearn-pi.ts` | `~/.pi/agent/extensions/` | system-prompt section (diff-patched by pi) | `pi -p --no-session -na` (one-shot, ephemeral) |

All adapters import one shared core (`plugin/autolearn-core.mjs`) that owns the methodology, so behavior is identical everywhere.

## How it works

1. **Conversation monitoring** — the harness adapter hooks session events, counting **user messages** and buffering the exchange (with secret redaction).
2. **Review spawning** — every N user messages (default 5), when the session goes quiet, or at session exit, the adapter spawns a review subprocess via a gated wrapper script.
3. **Learning extraction** — the review agent loads the single `autolearn` skill (reviewer mode), evaluates the conversation against a shared signal taxonomy, and records memories, user preferences, wiki patterns, and at most one skill change.
4. **Skill discovery** — agent-created skills are symlinked into `~/.agents/skills/` so every harness auto-discovers them.

```text
Any supported harness session
  └─ autolearn adapter (v1 / v2 / pi)
       ├─ every N user messages ──→ spawn review subprocess
       ├─ session quiet ──────────→ spawn review subprocess
       └─ session exit ───────────→ flush buffer as exit review
                                     │
                            review subprocess (one-shot)
                                     │
                      loads the autolearn skill, reviewer mode
                                     │
                      ┌──────────────┼──────────────┐
                      │              │              │
                  personas/      personas/       personas/
                  default/       default/        default/
                  memory.context user registry   skills/
                  (injected into  (preferences)   (symlinked to
                   every session)                 ~/.agents/skills/)
```

## Install

### One-liner (recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/ericmjl/agent-autolearn/main/install.sh | bash
```

This copies the adapters, installs the skill, patches your `opencode.json` (OpenCode only), and initializes the store. pi needs no config — the extension is auto-discovered from `~/.pi/agent/extensions/`.

### Manual install

```bash
git clone https://github.com/ericmjl/agent-autolearn.git
bash agent-autolearn/install.sh
```

### What the installer does

1. Copies `plugin/autolearn.js` (v1), `plugin/autolearn-v2.js` (v2), and `plugin/autolearn-core.mjs` (shared) to `~/.config/opencode/plugins/`, plus `plugin/autolearn-pi.ts` and the shared core to `~/.pi/agent/extensions/`
2. Installs the single consolidated `autolearn` skill (reviewer/observer/curator references + both CLIs) to `~/.agents/skills/autolearn/`, replacing the three legacy skill dirs
3. Patches `~/.config/opencode/opencode.json` to register the v1 plugin under `plugin`, the v2 plugin under `plugins`, the instructions entry, and the reviewer agent
4. Runs `autolearn.py init`, `retention score`, and `memory compose` to create and bootstrap `~/.autolearn/personas/default/`

### Verify

After installing, restart OpenCode / pi to load the adapter. With `opencode2` you can hot-reload in place — `opencode2 service restart` reloads plugins/config without quitting open sessions (note: running it from inside an agent session cancels that in-flight shell command, but the session survives and reconnects). You can confirm the store with:

```bash
uv run ~/.agents/skills/autolearn/scripts/autolearn.py memory list
```

In pi, run `/autolearn-review` inside a session to force a review of the current conversation buffer.

### Manual opencode.json config

If you prefer to edit `~/.config/opencode/opencode.json` yourself, here is what the installer adds. The `plugin` key is read by v1 (v2 ignores-then-normalizes it); the `plugins` key is native v2 (v1 ignores unknown keys). One file serves both versions:

```json
{
  "plugin": ["./plugins/autolearn.js"],
  "plugins": ["./plugins/autolearn-v2.js"],
  "instructions": ["~/.autolearn/personas/default/memory.context.md"],
  "agent": {
    "autolearn-reviewer": {
      "description": "Reviews past conversations for self-improvement opportunities",
      "hidden": true,
      "steps": 20,
      "prompt": "Load the autolearn skill and follow references/reviewer.md to review the attached conversation for learning opportunities. Take immediate action: record observations, update memory, create or patch skills.",
      "permission": {
        "bash": "allow", "read": "allow", "glob": "allow", "grep": "allow",
        "write": "allow", "edit": "deny", "webfetch": "deny", "task": "deny",
        "skill": "allow", "external_directory": "allow"
      }
    }
  }
}
```

## The enforced methodology (identical in every harness)

- **Review every N user messages** (default 5, `review_threshold`), counted in USER messages so a review always covers complete exchanges.
- **Idle reviews** on session quiet (`session_review_on_idle`, 5-min cooldown), **exit reviews** at session end (buffer flush, >2 messages).
- **Three-layer global throttle** (`plugin/autolearn-core.mjs`): identical conversation never reviews twice, `min_interval_ms` between review starts, `max_reviews_per_day` hard ceiling — plus wrapper-side gates (single-execution claim, interval, content hash) as defense in depth.
- **One skill change per review** (create OR patch), gated by a recurrence check — single-session patterns go to the wiki pattern notebook instead.
- **Reviewer recursion guards**: `AUTOLEARN_REVIEWER=1` env var, agent-name and title matching, and buffer depth guard.
- **Skill lifecycle**: curator transitions (stale at 30 days, archived at 90, pinned exempt); archives are never deleted.

## pi adapter notes

- Memory injection uses pi's `before_agent_start` system-prompt **sections**: pi diffs sections against what the model already has, so the first turn appends one patch message and the provider cache survives. No harness config file is patched.
- The review subprocess runs `pi -p --no-session -na`: one-shot print mode, ephemeral session, project-local resources ignored. The wrapper's opencode session-cleanup step is skipped entirely.
- Exit reviews fire on `session_shutdown` — including session switches (`/new`, `/resume`, `/fork`), which pi allows mid-process; the throttle gates keep the flush safe.
- `/autolearn-review` forces a review of the current buffer (still throttle-gated).
- The shared core keeps its `.mjs` extension so pi's extension discovery (`.ts`/`.js` only) never loads it as an extension.

## OpenCode v1 + v2 (beta) compatibility

OpenCode 2 is a breaking change for the plugin API only — v1 plugins (function exports) cannot load in v2, and vice versa. Autolearn therefore ships two thin shells over one shared core:

| File | Plugin API | Loaded by | How |
|------|-----------|-----------|-----|
| `plugin/autolearn.js` | v1 (`export default async (ctx) => ({ event })`) | `opencode` | `plugin` config key |
| `plugin/autolearn-v2.js` | v2 (`{ id, setup(ctx) }` + `ctx.event.subscribe`) | `opencode2` | `plugins` config key |
| `plugin/autolearn-pi.ts` | pi ExtensionAPI (`export default (pi) => {}`) | `pi` | extensions dir auto-discovery |
| `plugin/autolearn-core.mjs` | — (shared module) | all | imported by the shells |

Behavioral differences worth knowing:

- **Event mapping** — v2 emits `session.inbox.enqueued` (user text), `session.text.ended` (full assistant text), and `session.execution.succeeded|failed|interrupted` (turn boundary). The v2 shell uses the execution-end events as the idle signal, so quiet-session reviews still fire. pi uses `message_end` (per-message, with roles) and `agent_settled` (idle).
- **Review subprocess binary** — each adapter pins its own binary via `AUTOLEARN_HARNESS_BIN` (legacy `AUTOLEARN_OPENCODE_BIN` still honored). Reviews from a v2 session run via `opencode2` (session cleanup uses `opencode2 api delete` since v2 has no `session delete` CLI); reviews from pi run as one-shot `pi -p --no-session` processes with no cleanup.
- **Expected v2 warning** — v2 normalizes the v1 `plugin` key and will log one `failed to load plugin ... autolearn.js ... Expected object at ["default"]` warning per service start. This is expected and harmless: the `plugins` entry is what v2 actually loads. Remove the `plugin` entry once you retire v1.
- **Reviewer recursion guard** — in v2 the plugin runs inside the shared background service, so the env-var guard alone can't stop the reviewer's own sessions from being counted. The v2 shell also marks sessions by agent (`autolearn-reviewer`) and title (`autolearn*`) and skips them.
- **Exit reviews** — v1 reviews fire on process exit; v2's service outlives TUI sessions, so the v2 shell relies on threshold + turn-boundary reviews instead.

## Dependencies

- **[uv](https://docs.astral.sh/uv/)** — runs the Python CLI scripts with inline dependency resolution (no venv needed)
- **[Bun](https://bun.sh)** — runtime for the v1 plugin (bundled with OpenCode); the v2 plugin uses only Node-compatible APIs
- **Python ≥3.11** — for `autolearn.py` (dependencies resolved automatically via PEP 723 metadata)
- **OpenCode v1 (`opencode`) and/or v2 (`opencode2`), and/or [pi](https://github.com/earendil-works/pi-coding-agent)** — any combination; each loads its matching adapter

## Configuration

Config lives at `~/.autolearn/personas/default/config.yaml`:

```yaml
review_threshold: 5           # user messages (exchanges) between reviews
session_review_on_idle: true  # spawn review on session idle
max_conversation_buffer: 50   # max messages in buffer
curator_interval_days: 7      # how often to run curator
stale_after_days: 30          # days before skill → stale
archive_after_days: 90        # days before skill → archived
escalation_threshold: 3       # reinforcement count before curator suggests promotion to AGENTS.md
```

## CLI reference

Full command catalogue: [`skills/autolearn/references/cli.md`](skills/autolearn/references/cli.md).

```bash
AL=~/.agents/skills/autolearn/scripts

# Initialize the autolearn store
uv run $AL/autolearn.py init

# Memory (composed into memory.context.md, injected into every session)
uv run $AL/autolearn.py memory add "Use uv tool for Python CLI tools, never pip3 install"
uv run $AL/autolearn.py memory list
uv run $AL/autolearn.py memory strengthen <keyword>

# User profile (communication/workflow preferences)
uv run $AL/autolearn.py user add "Prefers concise responses"

# Skills
uv run $AL/autolearn.py skill create <name> "<description>"
uv run $AL/autolearn.py skill patch <name> <section> "<content>"
uv run $AL/autolearn.py skill list

# Curator (lifecycle management)
uv run $AL/autolearn.py curator run

# Session search (FTS5 over past harness conversations)
uv run $AL/autolearn.py search init
uv run $AL/autolearn.py search query "<terms>"

# Cross-machine sync (E2E-encrypted, opt-in)
uv run $AL/autolearn.py sync login
uv run $AL/autolearn.py sync push

# Behavioral rules + AGENTS.md escalation (improve.py)
uv run $AL/improve.py observe "<rule>" --project <name> [--domain <domain>]
uv run $AL/improve.py status
uv run $AL/improve.py due
uv run $AL/improve.py escalate --apply
```

Most `autolearn.py` commands accept `--persona <name>` to operate on a specific persona (default: machine-wide default or `default`). The plugin auto-syncs on session start and after reviews when `AUTOLEARN_SYNC_API_KEY` is set — see [Privacy](#privacy) and [`docs/designs/sync/`](docs/designs/sync/).

## Two CLIs, one skill

Both CLIs ship inside the single `autolearn` skill:

| CLI | Location | Store | Purpose |
|-----|----------|-------|---------|
| `autolearn.py` | `~/.agents/skills/autolearn/scripts/autolearn.py` | `~/.autolearn/` | Memory, skills, curator, wiki, search, sync |
| `improve.py` | `~/.agents/skills/autolearn/scripts/improve.py` | `~/.agent-improvement/rules.yaml` | Behavioral rule tracking and AGENTS.md escalation |

The observer mode records corrections in-session via `improve.py observe`; the reviewer does the same in batch (reviewer.md Step 4), and `improve.py escalate --apply` promotes repeated rules into the appropriate `AGENTS.md` file. Full escalation logic: [`skills/autolearn/references/observer.md`](skills/autolearn/references/observer.md).

## Data layout

```text
~/.autolearn/
├── personas/
│   └── default/               # default persona (no --persona flag)
│       ├── config.yaml            # thresholds and flags
│       ├── memory.context.md      # composed memory view (injected into every
│       │                          # session: opencode instructions / pi section)
│       ├── memories.jsonl         # memory registry (source of truth)
│       ├── user-profile.md        # user preferences
│       ├── observations.jsonl     # event log (auto-trimmed to 1000 lines)
│       ├── strengths.json         # reinforcement counters per memory entry
│       ├── reviews/               # generated review markdown files
│       ├── search.db              # FTS5 index over past sessions
│       ├── bin/                   # wrapper scripts (review-runner.sh)
│       ├── wiki/                  # pattern notebook + skill-impact ledger
│       ├── skills/                # agent-created skills
│       │   ├── {skill-name}/
│       │   │   └── SKILL.md
│       │   ├── .archive/
│       │   └── .usage.json
│       └── .curator_state.json
├── sync.yaml                  # sync config (server URL) — created by `sync login`
├── .encryption_salt           # per-installation salt for PBKDF2 key derivation
├── .persona_registry.json     # { name → uuid, sync_enabled } mapping
├── .default_persona           # machine-wide default persona name
└── debug.log                  # verbose adapter output (when AUTOLEARN_DEBUG=1)

~/.agents/skills/
├── autolearn/                 # THE skill: SKILL.md (shared taxonomy + mode router)
│   ├── references/            # observer.md, reviewer.md, curator.md, cli.md
│   └── scripts/               # autolearn.py + improve.py + helpers
└── {learned-skill} → ~/.autolearn/personas/default/skills/{learned-skill}/  # symlinks

~/.agent-improvement/
└── rules.yaml                 # improve.py rule store (observations, counts, written_to)
```

## Design docs

Full design documentation lives in `docs/`:

- [`docs/high-level-design.md`](docs/high-level-design.md) — architecture, decisions, risk matrix. Each feature and decision is marked `shipped`, `partial`, or `planned`.
- [`docs/designs/`](docs/designs/) — 8 LLDs and 11 EARS specifications covering all shipped features (conversation monitoring, knowledge store, skill management, review agent, session search, sync encryption, sync protocol, multi-persona). See [`docs/README.md`](docs/README.md) for the status-indexed overview.

## Running the curator on a schedule

```bash
# Weekly curator via opencode-scheduler (v1 `opencode schedule`;
# v2 beta has no schedule subcommand yet)
opencode schedule "autolearn-curator" --cron "0 3 * * 0" \
  --agent autolearn-reviewer \
  --prompt "Load the autolearn skill and follow references/curator.md to run the curator."
```

## Troubleshooting

**`opencode2` logs a plugin warning for autolearn.js.** Under v2, a warning like `failed to load plugin .../plugins/autolearn.js ... Expected object at ["default"]` appears once per service start. This is expected: v2 normalizes the v1 `plugin` key and cannot load v1-style plugins, so it skips the v1 shell and loads `./plugins/autolearn-v2.js` from the `plugins` key instead. Verify with `opencode2 plugin list` — the `autolearn-v2.js` entry must not be `(failed)`. Once you retire v1, remove the `plugin` entry from `opencode.json` to silence the warning.

**Reviews silently failing.** When a threshold- or idle-triggered review fails to spawn, the adapter saves the formatted review (context + conversation) to `~/.autolearn/review-failed-{timestamp}.md`. List recent failures with `ls ~/.autolearn/review-failed-*.md`. The error itself is logged to `~/.autolearn/debug.log` (when `AUTOLEARN_DEBUG=1` is set) and to stderr — read the most recent failure file to see which conversation triggered it.

**Enabling debug output.** Set `AUTOLEARN_DEBUG=1` before starting your harness to write verbose adapter output to `~/.autolearn/debug.log`.

**Recursive review spawning.** The adapters guard against this with an `AUTOLEARN_REVIEWER=1` environment variable in the spawned subprocess. If you see rapid-fire review entries in `observations.jsonl` seconds apart, verify this guard is in effect.

**Search index issues.** If `search query` returns empty results, the index may not have been built yet. Run `search init` once to populate it, or `search init --full` for a complete rebuild. Use `search status` to check index size and coverage.

**Manual verification.** Confirm the store is healthy:

```bash
uv run ~/.agents/skills/autolearn/scripts/autolearn.py memory list
uv run ~/.agents/skills/autolearn/scripts/autolearn.py search status
uv run ~/.agents/skills/autolearn/scripts/autolearn.py curator status
```

## Privacy

Autolearn records conversation excerpts locally to learn from them. By default **nothing leaves your machine**:

- All data lives under `~/.autolearn/` and `~/.agent-improvement/`.
- Messages are redacted of likely secrets (API keys, tokens, passwords) before buffering.
- The adapters and core CLI do not make outbound network requests. Sync is opt-in and E2E-encrypted: the adapter auto-pulls on session start and auto-pushes after reviews when `AUTOLEARN_SYNC_API_KEY` is set. Two interchangeable backends: **Fastify** (self-hosted, free, `sync-server/`) or **Convex** (managed, `sync-convex/`). See [`docs/high-level-design.md`](docs/high-level-design.md) Decisions 5–7.
- To wipe everything: `rm -rf ~/.autolearn ~/.agent-improvement` and remove the plugin/instructions entries from `~/.config/opencode/opencode.json` plus the two files from `~/.pi/agent/extensions/`.

## Uninstall

There is no automated uninstaller. To remove manually:

```bash
# Remove adapters and installed skill
rm ~/.config/opencode/plugins/autolearn.js \
   ~/.config/opencode/plugins/autolearn-v2.js \
   ~/.config/opencode/plugins/autolearn-core.mjs \
   ~/.pi/agent/extensions/autolearn-pi.ts \
   ~/.pi/agent/extensions/autolearn-core.mjs
rm -rf ~/.agents/skills/autolearn

# Remove local data stores (optional — keeps your learned memory/skills)
# rm -rf ~/.autolearn ~/.agent-improvement

# Edit ~/.config/opencode/opencode.json and remove the "autolearn.js" entry
# from "plugin", the "autolearn-v2.js" entry from "plugins", the
# "~/.autolearn/personas/default/memory.context.md" instructions entry, and
# the "autolearn-reviewer" agent entry.
```

## License

MIT
