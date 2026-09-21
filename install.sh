#!/usr/bin/env bash
# agent-autolearn installer (multi-harness: OpenCode v1, OpenCode v2, pi)
#
# Usage:
#   bash install.sh                    # run from cloned repo
#   bash install.sh /path/to/repo      # specify repo path
#
# Or one-liner:
#   curl -fsSL https://raw.githubusercontent.com/ericmjl/opencode-autolearn/main/install.sh | bash
#
# Installs for every harness detected on the machine:
#   - OpenCode v1/v2: plugins + opencode.json patch
#   - pi:             extension + shared core in ~/.pi/agent/extensions/
# The skills + store (~/.autolearn) are shared by all harnesses.

set -euo pipefail

REPO_DIR="${1:-$(cd "$(dirname "$0")" && pwd)}"

if [[ ! -f "$REPO_DIR/plugin/autolearn.js" ]]; then
  echo "Error: could not find plugin/autolearn.js in $REPO_DIR"
  echo "Clone the repo first: git clone https://github.com/ericmjl/opencode-autolearn.git"
  exit 1
fi

PLUGIN_DIR="$HOME/.config/opencode/plugins"
PI_EXT_DIR="$HOME/.pi/agent/extensions"
SKILLS_DIR="$HOME/.agents/skills"
OPENCODE_JSON="$HOME/.config/opencode/opencode.json"

echo "agent-autolearn installer"
echo "========================"
echo ""

# 1. Copy plugins/extensions. The core uses a .mjs extension so OpenCode v2's
# plugins/ auto-discovery and pi's extensions/ discovery do not try to load
# it as a plugin (both only discover .js/.ts files).
echo "[1/5] Installing plugins/extensions..."
mkdir -p "$PLUGIN_DIR"
cp "$REPO_DIR/plugin/autolearn.js" "$PLUGIN_DIR/"
cp "$REPO_DIR/plugin/autolearn-v2.js" "$PLUGIN_DIR/"
cp "$REPO_DIR/plugin/autolearn-core.mjs" "$PLUGIN_DIR/"
# Remove any stray pre-.mjs core copy from earlier installs (the .js name
# would be picked up by OpenCode v2's plugins/ auto-discovery and fail
# schema validation with a warning).
rm -f "$PLUGIN_DIR/autolearn-core.js"

# pi extension (both files land in the extensions dir; the core's .mjs name
# keeps pi's discovery from loading it as an extension, while the shell's
# relative import still resolves).
mkdir -p "$PI_EXT_DIR"
cp "$REPO_DIR/plugin/autolearn-pi.ts" "$PI_EXT_DIR/"
cp "$REPO_DIR/plugin/autolearn-core.mjs" "$PI_EXT_DIR/"

# 2. Install the single consolidated skill, replacing the three legacy skill
# dirs (autolearn-reviewer, autolearn-curator, self-improving-agent). Only
# removes the legacy dirs this installer owns; never touches agent-created
# skills (e.g. autolearn-audit) or user-installed skills.
echo "[2/5] Installing skills..."
mkdir -p "$SKILLS_DIR"
rm -rf "$SKILLS_DIR/autolearn-reviewer" "$SKILLS_DIR/autolearn-curator" "$SKILLS_DIR/self-improving-agent"
rm -rf "$SKILLS_DIR/autolearn"
cp -r "$REPO_DIR/skills/autolearn" "$SKILLS_DIR/"

# 3. Patch opencode.json
echo "[3/5] Configuring opencode.json..."
mkdir -p "$(dirname "$OPENCODE_JSON")"

python3 -c "
import json, os, sys

path = '$OPENCODE_JSON'

if os.path.exists(path):
    with open(path) as f:
        data = json.load(f)
else:
    data = {}

changed = False

# plugin — v1 shell (read by OpenCode v1 via the 'plugin' key; OpenCode v2
# normalizes the same key, where the v1 function export is expected to fail
# schema validation with a warning — harmless, the v2 entry below is what
# v2 actually loads).
if 'plugin' not in data:
    data['plugin'] = []
plugins = data['plugin'] if isinstance(data['plugin'], list) else [data['plugin']]
plugin_entry = './plugins/autolearn.js'
if plugin_entry not in plugins:
    plugins.append(plugin_entry)
    data['plugin'] = plugins
    changed = True

# plugins — v2 shell (native v2 key; OpenCode v1 ignores unknown top-level
# keys, so both versions can share this file).
if 'plugins' not in data:
    data['plugins'] = []
plugins_v2 = data['plugins'] if isinstance(data['plugins'], list) else [data['plugins']]
plugin_v2_entry = './plugins/autolearn-v2.js'
if plugin_v2_entry not in plugins_v2:
    plugins_v2.append(plugin_v2_entry)
    data['plugins'] = plugins_v2
    changed = True

# instructions — point at the generated context view (Memory Insight);
# strip any superseded memory.md paths (persona + flat-layout).
if 'instructions' not in data:
    data['instructions'] = []
instructions = data['instructions'] if isinstance(data['instructions'], list) else [data['instructions']]
ctx_entry = os.path.expanduser('~/.autolearn/personas/default/memory.context.md')
# Strip superseded memory.md paths (persona + flat layout), matching both
# expanded absolute paths and literal-tilde forms.
superseded = {
    os.path.expanduser('~/.autolearn/personas/default/memory.md'),
    os.path.expanduser('~/.autolearn/memory.md'),
    '~/.autolearn/personas/default/memory.md',
    '~/.autolearn/memory.md',
}
if any(i in superseded for i in instructions):
    instructions = [i for i in instructions if i not in superseded]
    data['instructions'] = instructions
    changed = True
if ctx_entry not in instructions:
    instructions.append(ctx_entry)
    data['instructions'] = instructions
    changed = True

# agent — the reviewer role the plugin spawns via 'run --agent'. The prompt
# points at the consolidated autolearn skill (reviewer reference).
if 'agent' not in data:
    data['agent'] = {}
reviewer_prompt = (
    'Load the autolearn skill and follow references/reviewer.md to review '
    'the attached conversation for learning opportunities. Take immediate '
    'action: record observations, update memory, create or patch skills.'
)
if 'autolearn-reviewer' not in data['agent']:
    data['agent']['autolearn-reviewer'] = {
        'description': 'Reviews past conversations for self-improvement opportunities',
        'hidden': True,
        'steps': 20,
        'prompt': reviewer_prompt,
        'permission': {
            'bash': 'allow',
            'read': 'allow',
            'glob': 'allow',
            'grep': 'allow',
            'write': 'allow',
            'edit': 'deny',
            'webfetch': 'deny',
            'task': 'deny',
            'skill': 'allow',
            'external_directory': 'allow'
        }
    }
    changed = True
elif data['agent']['autolearn-reviewer'].get('prompt') != reviewer_prompt:
    data['agent']['autolearn-reviewer']['prompt'] = reviewer_prompt
    changed = True

with open(path, 'w') as f:
    json.dump(data, f, indent=2)
    f.write('\n')

if changed:
    print('  Updated ' + path)
else:
    print('  Already configured (' + path + ')')
" 2>&1

# 4. Initialize + bootstrap the registry (migrates legacy memory.md if present)
echo "[4/5] Initializing autolearn store..."
CLI="$SKILLS_DIR/autolearn/scripts/autolearn.py"
if [[ ! -f "$CLI" ]]; then
  CLI="$SKILLS_DIR/autolearn-reviewer/scripts/autolearn.py"
fi
if command -v uv &>/dev/null; then
    uv run "$CLI" init
    uv run "$CLI" retention score   # migrates legacy store -> registry, scores tiers
    uv run "$CLI" memory compose    # generate memory.context.md from the registry
else
    echo "  Warning: uv not found. Run manually: uv run $CLI init"
fi

# 5. Verify
echo "[5/5] Verifying..."
OK=true
[[ -f "$PLUGIN_DIR/autolearn.js" ]] || { echo "  MISSING: $PLUGIN_DIR/autolearn.js"; OK=false; }
[[ -f "$PLUGIN_DIR/autolearn-v2.js" ]] || { echo "  MISSING: $PLUGIN_DIR/autolearn-v2.js"; OK=false; }
[[ -f "$PLUGIN_DIR/autolearn-core.mjs" ]] || { echo "  MISSING: $PLUGIN_DIR/autolearn-core.mjs"; OK=false; }
[[ -f "$PI_EXT_DIR/autolearn-pi.ts" ]] || { echo "  MISSING: $PI_EXT_DIR/autolearn-pi.ts"; OK=false; }
[[ -f "$PI_EXT_DIR/autolearn-core.mjs" ]] || { echo "  MISSING: $PI_EXT_DIR/autolearn-core.mjs"; OK=false; }
[[ -f "$SKILLS_DIR/autolearn/SKILL.md" ]] || { echo "  MISSING: skills/autolearn"; OK=false; }
[[ -f "$SKILLS_DIR/autolearn/scripts/autolearn.py" ]] || { echo "  MISSING: skills/autolearn/scripts/autolearn.py"; OK=false; }
[[ -f "$SKILLS_DIR/autolearn/scripts/improve.py" ]] || { echo "  MISSING: skills/autolearn/scripts/improve.py"; OK=false; }
[[ -f "$HOME/.autolearn/personas/default/memory.context.md" ]] || { echo "  MISSING: ~/.autolearn/personas/default/memory.context.md"; OK=false; }
[[ -f "$HOME/.autolearn/personas/default/memories.jsonl" ]] || { echo "  MISSING: ~/.autolearn/personas/default/memories.jsonl"; OK=false; }

echo ""
if $OK; then
    echo "Done! Autolearn will activate on your next OpenCode or pi session."
else
    echo "Some files are missing — check the errors above."
    exit 1
fi
