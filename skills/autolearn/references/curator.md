# Curator Mode

You are the autolearn curator. Your job is to review the skill library at
`$HOME/.autolearn/skills/` and maintain its health. This mode runs as a
scheduled job (weekly by default) or manually when the library feels
cluttered.

CLI: `$HOME/.agents/skills/autolearn/scripts/autolearn.py`

## What You Do

### Step 1: Run the automated curator

```bash
uv run $HOME/.agents/skills/autolearn/scripts/autolearn.py curator run
```

Automatic state transitions:

- Skills with no activity for 30 days become `stale`
- Skills with no activity for 90 days become `archived`
- Pinned skills are exempt

### Step 2: Review the skill library

```bash
uv run $HOME/.agents/skills/autolearn/scripts/autolearn.py skill list
uv run $HOME/.agents/skills/autolearn/scripts/autolearn.py skill usage
```

Look for:

1. **Prefix clusters**: multiple skills sharing a domain keyword
   (e.g., "python-error-handling", "python-testing", "python-style")
2. **Narrow skills**: very specific scope that could be sections of a
   broader skill
3. **Stale skills**: marked `stale` but could be revived
4. **Duplicate content**: skills that overlap significantly

### Step 3: Consolidate (if needed)

For each cluster of narrow skills:

1. Read each skill's SKILL.md
2. Create an umbrella skill that covers the domain
3. Move the best content from each narrow skill into the umbrella
4. Archive the narrow skills:

```bash
uv run $HOME/.agents/skills/autolearn/scripts/autolearn.py skill archive <narrow-skill-name>
```

### Step 4: Report

```text
Curator report:
- Auto-transitions: N stale, M archived
- Consolidated: X narrow skills into Y umbrellas
- Skills library: A active, B stale, C archived
```

## Rules

- Never delete skills. Only archive them. Archives are always recoverable.
- Only consolidate skills created by autolearn (`created_by: autolearn`).
- Never touch user-installed or bundled skills.
- If unsure whether to consolidate, leave as-is.
- Keep the umbrella skill's SKILL.md under 3000 characters.
- After consolidation, update any scheduled jobs that referenced old names.

## Scheduling

Weekly cron example (OpenCode scheduler):

```bash
opencode schedule "autolearn-curator" --cron "0 3 * * 0"
--agent autolearn-reviewer
--prompt "Load the autolearn skill and follow references/curator.md to run the curator."
```
