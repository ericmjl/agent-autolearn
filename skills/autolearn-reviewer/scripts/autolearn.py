"""Autolearn CLI - manages ~/.autolearn/ store for self-improvement.

Commands:
  memory add/remove/list/strengths   Manage persistent memory
  user add/remove/list               Manage user profile
  skill create/patch/archive/list    Manage agent-created skills
  skill usage                        Show skill usage telemetry
  curator run/status                 Run skill consolidation and cleanup
  search init/query/sessions/status  FTS5 full-text search over past sessions
  sync login/logout/export-key       E2E-encryption key management
  sync push/pull/status              Cross-machine sync (default persona only)
  init                               Initialize the autolearn store
"""

# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pyyaml",
#     "python-slugify",
#     "cryptography",
#     "keyring",
#     "keyrings.alt",  # PlaintextKeyring file backend for headless hosts w/o OS keychain
#     "requests",
# ]
# ///
from __future__ import annotations

import argparse
import getpass
import json
import os
import sqlite3
import sys
import time
import uuid as uuid_mod
from datetime import date, datetime, timezone
from pathlib import Path

try:
    import requests
    RequestsError = requests.RequestException
except ImportError:  # optional dep: only needed for sync; tests/CLI import fine without it
    requests = None  # type: ignore[assignment]
    RequestsError = OSError

try:
    import yaml
except ImportError:  # config/frontmatter parsing needs yaml at call time; import stays light
    yaml = None  # type: ignore[assignment]
from slugify import slugify as python_slugify

# Make sync_crypto.py importable when run via uv run / pytest.
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import sync_crypto
except ImportError:  # cryptography only needed for sync; module stays importable without it
    sync_crypto = None  # type: ignore[assignment]

# Memory Insight subsystem (registry + retention + composer + shift + ui).
# These are siblings in the same scripts dir; imported here so the CLI can
# dispatch to them. See docs/designs/memory-insight/LLD.md.
import registry
import retention
import composer
import shift
import inspector_server

# Certified Procedures subsystem (outcome index + falsify + shortcuts).
# See docs/designs/certified-procedures/LLD.md.
import outcomes
import falsify
import shortcuts

# Long-horizon skill loop (proposer + pruner).
# See docs/designs/long-horizon-skills/LLD.md.
import proposer

# Wiki layer (persistent pattern store + skill-impact ledger).
# See docs/designs/wiki-layer/LLD.md.
import wiki as wiki_layer

# @spec KS-MEM-020
DATA_HOME = Path(os.environ.get("AUTOLEARN_HOME", Path.home() / ".autolearn"))

# --- Persona-aware directory layout (Phase 3) ---
# @spec SYNC-PER-006, SYNC-PER-007
PERSONAS_DIR = DATA_HOME / "personas"
REGISTRY_FILE = DATA_HOME / ".persona_registry.json"
ACTIVE_PERSONA = "default"
ACTIVE_PERSONA_DIR = PERSONAS_DIR / ACTIVE_PERSONA

# File paths for the active persona. Reassigned by set_persona().
# @spec SYNC-PER-008
MEMORY_FILE = ACTIVE_PERSONA_DIR / "memory.md"
USER_FILE = ACTIVE_PERSONA_DIR / "user-profile.md"
CONFIG_FILE = ACTIVE_PERSONA_DIR / "config.yaml"
SKILLS_DIR = ACTIVE_PERSONA_DIR / "skills"
ARCHIVE_DIR = SKILLS_DIR / ".archive"
USAGE_FILE = SKILLS_DIR / ".usage.json"
CURATOR_STATE_FILE = ACTIVE_PERSONA_DIR / ".curator_state.json"
OBSERVATIONS_FILE = ACTIVE_PERSONA_DIR / "observations.jsonl"
AGENTS_SKILLS_DIR = Path(os.environ.get("AGENTS_SKILLS_DIR", Path.home() / ".agents" / "skills"))

MAX_MEMORY_CHARS = 3000
MAX_USER_CHARS = 2000

OPENCODE_DB = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
SEARCH_DB = ACTIVE_PERSONA_DIR / "search.db"
OUTCOMES_DB = ACTIVE_PERSONA_DIR / "outcomes.db"  # Certified Procedures spine (persona-local)

# Sync config and salt are shared across personas (not persona-specific).
SYNC_CONFIG_FILE = DATA_HOME / "sync.yaml"
SALT_FILE = DATA_HOME / ".encryption_salt"


# @spec SYNC-PER-006
def set_persona(name: str) -> None:
    """Reassign all module-level file paths to point at personas/<name>/."""
    global ACTIVE_PERSONA, ACTIVE_PERSONA_DIR
    global MEMORY_FILE, USER_FILE, CONFIG_FILE, SKILLS_DIR, ARCHIVE_DIR
    global USAGE_FILE, CURATOR_STATE_FILE, OBSERVATIONS_FILE
    global SEARCH_DB, OUTCOMES_DB
    ACTIVE_PERSONA = name
    ACTIVE_PERSONA_DIR = PERSONAS_DIR / name
    MEMORY_FILE = ACTIVE_PERSONA_DIR / "memory.md"
    USER_FILE = ACTIVE_PERSONA_DIR / "user-profile.md"
    CONFIG_FILE = ACTIVE_PERSONA_DIR / "config.yaml"
    SKILLS_DIR = ACTIVE_PERSONA_DIR / "skills"
    ARCHIVE_DIR = SKILLS_DIR / ".archive"
    USAGE_FILE = SKILLS_DIR / ".usage.json"
    CURATOR_STATE_FILE = ACTIVE_PERSONA_DIR / ".curator_state.json"
    OBSERVATIONS_FILE = ACTIVE_PERSONA_DIR / "observations.jsonl"
    SEARCH_DB = ACTIVE_PERSONA_DIR / "search.db"
    OUTCOMES_DB = ACTIVE_PERSONA_DIR / "outcomes.db"


# @spec KS-MEM-001
def ensure_dirs():
    DATA_HOME.mkdir(parents=True, exist_ok=True)
    migrate_to_personas()
    init_registry()
    ACTIVE_PERSONA_DIR.mkdir(parents=True, exist_ok=True)
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)


# --- Persona migration + registry ---

FLAT_FILES = [
    "memory.md", "user-profile.md", "config.yaml", "observations.jsonl",
    "search.db", ".curator_state.json",
    "memories.jsonl", "topics.jsonl", "candidates.jsonl", "memory.context.md",
]


def migrate_to_personas() -> None:
    """One-time migration from flat ~/.autolearn/ to personas/default/.

    Idempotent: skips if personas/ already exists.
    """
    if PERSONAS_DIR.exists():
        return
    has_flat = any((DATA_HOME / f).exists() for f in FLAT_FILES)
    if not has_flat and not (DATA_HOME / "skills").exists():
        return  # new install, nothing to migrate

    default_dir = PERSONAS_DIR / "default"
    default_dir.mkdir(parents=True, exist_ok=True)

    for f in FLAT_FILES:
        src = DATA_HOME / f
        if src.exists() and not src.is_dir():
            try:
                src.rename(default_dir / f)
            except OSError:
                pass

    for d in ("skills", "reviews", "bin"):
        src_dir = DATA_HOME / d
        if src_dir.exists() and src_dir.is_dir() and not (default_dir / d).exists():
            try:
                src_dir.rename(default_dir / d)
            except OSError:
                pass

    init_registry()
    print(f"[autolearn] Migrated flat layout to {default_dir}")


def init_registry() -> None:
    """Create .persona_registry.json with the default persona if absent."""
    if REGISTRY_FILE.exists():
        return
    registry = {
        "default": {
            "uuid": None,  # resolved lazily from salt on first sync
            "description": "Default knowledge store",
            "sync_enabled": True,
            "created_at": date.today().isoformat(),
        }
    }
    save_registry(registry)


def load_registry() -> dict:
    if REGISTRY_FILE.exists():
        try:
            return json.loads(REGISTRY_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_registry(registry: dict) -> None:
    DATA_HOME.mkdir(parents=True, exist_ok=True)
    REGISTRY_FILE.write_text(json.dumps(registry, indent=2) + "\n")


def resolve_persona_uuid(name: str, salt: bytes) -> str:
    """Return the UUID for a persona, creating the registry entry if needed.

    For 'default': uses sync_crypto.default_persona_id(salt) so existing Phase 1
    encrypted blobs stay decryptable. For other personas: random UUID v4.
    """
    registry = load_registry()
    entry = registry.get(name)
    if entry and entry.get("uuid"):
        return entry["uuid"]
    if name == "default":
        resolved = sync_crypto.default_persona_id(salt)
    else:
        resolved = str(uuid_mod.uuid4())
    if entry:
        entry["uuid"] = resolved
    else:
        registry[name] = {
            "uuid": resolved,
            "description": "",
            "sync_enabled": True,
            "created_at": date.today().isoformat(),
        }
    save_registry(registry)
    return resolved


def read_md(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text()


def write_md(path: Path, content: str):
    ensure_dirs()
    path.write_text(content)


def load_usage() -> dict:
    if USAGE_FILE.exists():
        try:
            return json.loads(USAGE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_usage(data: dict):
    ensure_dirs()
    USAGE_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


# @spec CP-OUT-005, CP-INT-001, SM-LC-013, SM-LC-014, SM-LC-015, SM-LC-016
def repair_skill_use_counts() -> dict:
    """Write skill load signals (derived from the outcome index) into .usage.json.

    Two phases:

    1. **Update existing entries.** For every skill already in ``.usage.json``,
       copy the latest ``use_count`` from the outcome index and refresh
       ``last_activity_at`` if the index observes a more recent load than the
       recorded date. ``created_by`` is never modified (SM-LC-016).

    2. **Add ``tracked-manual`` entries for previously-untracked skills.**
       Scan configured skill-discovery dirs for ``SKILL.md`` files. For any
       on-disk skill that is (a) not already in ``.usage.json``, (b) not in an
       ``.archive*`` directory (SM-LC-015), and (c) has been loaded at least
       once (count > 0 in the outcome index), add a tracking entry with
       ``created_by: "tracked-manual"`` (SM-LC-014).

    Returns a summary dict ``{"updated": N, "added": M, "skills_with_loads": K}``.
    """
    usage = load_usage()
    idx = outcomes.OutcomeIndex(OUTCOMES_DB, OPENCODE_DB)
    signals = idx.skill_use_signals()
    idx.close()

    # Phase 1 — refresh use_count + last_activity_at on existing entries.
    updated = 0
    for name, meta in usage.items():
        sig = signals.get(name)
        if sig is None:
            continue
        dirty = False
        new_count = sig["count"]
        if meta.get("use_count") != new_count:
            meta["use_count"] = new_count
            dirty = True
        # Refresh last_activity_at if the index sees a more recent load.
        last_ms = sig["last_seen_ms"]
        if last_ms:
            last_iso = datetime.fromtimestamp(last_ms / 1000).strftime("%Y-%m-%d")
            if meta.get("last_activity_at", "") < last_iso:
                meta["last_activity_at"] = last_iso
                dirty = True
        if dirty:
            updated += 1

    # Phase 2 — add tracked-manual entries for on-disk skills (SM-LC-013/014/015).
    added = 0
    on_disk = _scan_skill_dirs_for_repair()
    for name, created_at_iso in on_disk.items():
        if name in usage:
            continue  # SM-LC-016: never touch existing entries
        sig = signals.get(name)
        if sig is None or sig["count"] == 0:
            continue  # never loaded, no signal worth tracking
        last_ms = sig["last_seen_ms"]
        last_iso = (
            datetime.fromtimestamp(last_ms / 1000).strftime("%Y-%m-%d")
            if last_ms else created_at_iso
        )
        usage[name] = {
            "created_by": "tracked-manual",
            "created_at": created_at_iso,
            "use_count": sig["count"],
            "patch_count": 0,
            "last_activity_at": last_iso,
            "state": "active",
            "pinned": False,
        }
        added += 1

    if updated or added:
        save_usage(usage)
    return {
        "updated": updated,
        "added": added,
        "skills_with_loads": len(signals),
    }


# Skill discovery roots — mirrors opencode's own skill-discovery scan so that
# `repair_skill_use_counts()` phase 2 sees the same skills the agent sees.
# Override via the ``AUTOLEARN_SKILL_DISCOVERY`` env var (``:``-separated) for
# test isolation.
# @spec SM-LC-013
def _skill_discovery_dirs() -> list:
    env = os.environ.get("AUTOLEARN_SKILL_DISCOVERY")
    if env:
        return [Path(p) for p in env.split(os.pathsep) if p]
    return [
        Path.home() / ".agents" / "skills",
        Path.home() / ".config" / "opencode" / "skills",
    ]


# @spec SM-LC-013, SM-LC-015
def _scan_skill_dirs_for_repair() -> dict:
    """Return ``{skill_name: created_at_iso}`` for skills on disk, excluding archives.

    Iterates every configured skill-discovery directory. For each subdirectory
    containing a ``SKILL.md``:

    - skips any directory whose name starts with ``.archive`` (SM-LC-015) —
      this covers ``.archive/`` (autolearn-managed) and ``.archive-manual/``
      (ad-hoc user archival) alike, so neither is resurrected into
      ``.usage.json``;
    - records ``created_at`` as the ``SKILL.md`` file mtime (best available
      proxy for "when this skill was installed");
    - first-seen-wins precedence across discovery dirs (a skill shadowed by an
      earlier dir is not overwritten).
    """
    found: dict[str, str] = {}
    for d in _skill_discovery_dirs():
        if not d.is_dir():
            continue
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir():
                continue
            # SM-LC-015: skip any archive variant.
            if entry.name.startswith(".archive"):
                continue
            skill_md = entry / "SKILL.md"
            if not skill_md.exists():
                continue
            if entry.name in found:
                continue  # first-seen-wins
            try:
                mtime = skill_md.stat().st_mtime
                mtime_iso = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
            except OSError:
                mtime_iso = date.today().isoformat()
            found[entry.name] = mtime_iso
    return found


# @spec LH-PROP-001..003 — full proposer loop as a single CLI entry point
def cmd_proposals_scan(args):
    """scan -> verify -> promote. Self-contained for frequent (scheduled) runs."""
    summary = proposer.scan(ACTIVE_PERSONA_DIR)
    print(f"Scanned {summary['sessions_scanned']} sessions: "
          f"{summary['new']} new proposals, {summary['updated']} updated "
          f"({summary['proposals_total']} total).")
    res = proposer.verify_pending(ACTIVE_PERSONA_DIR)
    print(f"Verified {res['checked']} unverified proposals: "
          f"{res['verified']} passed (auto-promote-ready).")
    promo = promote_proposals()
    if promo["created"]:
        print(f"Promoted {len(promo['created'])} new skill(s): {', '.join(promo['created'])}")
    if promo["skipped_already_existed"]:
        print(f"({len(promo['skipped_already_existed'])} proposal(s) matched an existing skill)")


# @spec LH-PROP-003 — auto-promote verified proposals to skills (gated by falsify)
def promote_proposals() -> dict:
    """Create skills for proposals whose common_resolution passed falsification.

    The created skill carries a ``verify:`` block (the common_resolution), so the
    new skill is itself falsifiable. Mirrors ``cmd_skill_create`` but is driven by
    proposals and marks each consumed proposal ``promoted``.
    """
    proposals = proposer._load(ACTIVE_PERSONA_DIR)
    ready = [p for p in proposals.values()
             if p.get("status") == "pending" and p.get("verified") is True]
    created, skipped = [], []
    usage = load_usage()
    for p in ready:
        name = slugify((p.get("request_summary") or p.get("common_resolution") or "skill"))[:60]
        if not name:
            name = "skill"
        skill_dir = SKILLS_DIR / name
        skill_file = skill_dir / "SKILL.md"
        if skill_file.exists() or name in usage:
            p["status"] = "promoted"
            p["promoted_skill"] = name
            skipped.append(name)
            continue
        skill_dir.mkdir(parents=True, exist_ok=True)
        desc = (p.get("request_summary") or "Auto-promoted from a recurring cross-session pattern.")[:200]
        cmd = p.get("common_resolution") or ""
        content = (
            f"---\nname: {name}\ndescription: |\n  {desc}\n"
            f"created_by: autolearn\ncreated_at: \"{date.today().isoformat()}\"\n"
            "verify:\n"
            f"  command: \"{cmd}\"\n"
            "  expect_exit: 0\n---\n\n"
            f"# {name.replace('-', ' ').title()}\n\n{desc}\n\n"
            "## Instructions\n\n"
            f"This skill was auto-promoted because users repeatedly requested it across "
            f"{p.get('sessions_count', 0)} sessions, and the direct invocation was "
            "verified to pass. Replace this TODO with concrete procedural steps.\n"
        )
        skill_file.write_text(content)
        usage[name] = {
            "created_by": "autolearn", "created_at": date.today().isoformat(),
            "use_count": 0, "patch_count": 0,
            "last_activity_at": date.today().isoformat(), "state": "active",
            "promoted_from": p.get("id"),
        }
        link_path = AGENTS_SKILLS_DIR / name
        AGENTS_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        if not link_path.exists():
            try:
                link_path.symlink_to(skill_dir)
            except OSError:
                pass
        p["status"] = "promoted"
        p["promoted_skill"] = name
        # @spec WIKI-IMP-003, WIKI-SKL-001 — ledger + provenance for promoted skills
        if wiki_layer.enabled(ACTIVE_PERSONA_DIR):
            try:
                wiki_layer.append_skill_impact(
                    ACTIVE_PERSONA_DIR, action="promote", skill=name,
                    summary=f"auto-promoted from proposal {p.get('id', '?')}: "
                            f"{(p.get('request_summary') or '')[:200]}",
                    outcome="accepted", trigger="proposer")
                wiki_layer.write_purpose(
                    skill_dir, origin=(p.get("request_summary") or
                                       "auto-promoted from a recurring pattern"),
                    patterns=[], evolution="auto-promoted by proposer")
            except Exception:
                pass
        created.append(name)
    save_usage(usage)
    proposer._save(ACTIVE_PERSONA_DIR, proposals)
    return {"created": created, "skipped_already_existed": skipped}


# @spec LH-PRUNE-001 — never-used pruner (use_count × age)
def prune_unused_skills(config: dict) -> dict:
    """Auto-archive autolearn-created skills with use_count==0 past the grace period.

    Uses the repaired use_count (derived from outcomes index skill-load counts).
    Pinned skills and non-autolearn skills are exempt.
    """
    grace = int(config.get("unused_grace_days", 60))
    today = date.today()
    usage = load_usage()
    archived = []
    for name, meta in list(usage.items()):
        if meta.get("state") == "archived" or meta.get("pinned"):
            continue
        if meta.get("created_by") != "autolearn":
            continue
        use_count = int(meta.get("use_count") or 0)
        if use_count != 0:
            continue
        created_str = meta.get("created_at") or meta.get("last_activity_at")
        if not created_str:
            continue
        try:
            age_days = (today - date.fromisoformat(created_str)).days
        except ValueError:
            continue
        if age_days <= grace:
            continue
        skill_dir = SKILLS_DIR / name
        if skill_dir.exists() and not (ARCHIVE_DIR / name).exists():
            ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
            try:
                skill_dir.rename(ARCHIVE_DIR / name)
            except OSError:
                continue
        meta["state"] = "archived"
        meta["archived_at"] = today.isoformat()
        archived.append(name)
    if archived:
        save_usage(usage)
    return {"archived": archived, "grace_days": grace}


# @spec WIKI-CUR-001 — wiki consolidation (curator pass)
def consolidate_wiki(config: dict) -> dict:
    """Write a tombstone for stale patterns superseded by a skill.

    A pattern untouched longer than ``stale_after_days`` whose topic is
    represented by a promoted skill gets a one-line tombstone in
    ``wiki/consolidated/`` and loses its index entry. The pattern page
    itself is never deleted (the wiki is append-only history).
    """
    stale_days = int(config.get("stale_after_days", 30))
    today = date.today()
    if not wiki_layer.enabled(ACTIVE_PERSONA_DIR):
        return {"consolidated": []}
    usage = load_usage()
    promoted = {m.get("promoted_from") for m in usage.values()
                if m.get("promoted_from")}
    active_skill_names = {n for n, m in usage.items()
                          if m.get("created_by") == "autolearn"
                          and m.get("state") != "archived"}
    consolidated = []
    for p in wiki_layer.list_patterns(ACTIVE_PERSONA_DIR):
        slug, updated = p["slug"], p.get("updated", "unknown")
        try:
            days = (today - date.fromisoformat(updated)).days
        except ValueError:
            continue  # unparseable date -> leave alone
        if days < stale_days:
            continue
        # superseded heuristic: an active autolearn skill exists whose name
        # shares >= 2 tokens with the slug
        slug_toks = wiki_layer.tokens(slug.replace("-", " "))
        matched = _skills_matching_slug(active_skill_names, slug_toks)
        if not matched:
            continue
        tomb = wiki_layer.consolidated_dir(ACTIVE_PERSONA_DIR) / f"{slug}.tombstone.md"
        if not tomb.exists():
            tomb.write_text(
                f"- {today.isoformat()}: consolidated {slug} -> skill "
                f"'{matched[0]}' (superseded; page retained in patterns/)\n",
                encoding="utf-8")
        wiki_layer.remove_index_entry(ACTIVE_PERSONA_DIR, slug)
        consolidated.append(slug)
    return {"consolidated": consolidated}


def _skills_matching_slug(skill_names: set, slug_toks: set) -> list[str]:
    toks = {t for t in slug_toks if len(t) >= 3}
    out = []
    for n in skill_names:
        n_toks = wiki_layer.tokens(n.replace("-", " "))
        if len(toks & n_toks) >= 2:
            out.append(n)
    return out


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return yaml.safe_load(CONFIG_FILE.read_text()) or {}
        except Exception:
            pass
    return {}


def load_curator_state() -> dict:
    if CURATOR_STATE_FILE.exists():
        try:
            return json.loads(CURATOR_STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"last_run": None, "runs": []}


def save_curator_state(state: dict):
    ensure_dirs()
    CURATOR_STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False) + "\n"
    )


def slugify(text: str) -> str:
    return python_slugify(text, max_length=60)


# @spec KS-MEM-015, KS-MEM-016, KS-MEM-017
def extract_entries(md: str) -> list[str]:
    entries = []
    in_comment = False
    for line in md.splitlines():
        stripped = line.strip()
        if stripped.startswith("<!--"):
            in_comment = True
        if in_comment:
            if "-->" in stripped:
                in_comment = False
            continue
        if stripped.startswith("#") or stripped.startswith("<!--") or not stripped:
            continue
        if stripped.startswith("- ") or stripped.startswith("* "):
            entries.append(stripped[2:].strip())
        elif stripped and not stripped.startswith("#"):
            entries.append(stripped)
    return entries


# @spec KS-MEM-018, KS-MEM-019
def entries_to_md(entries: list[str], header: str) -> str:
    md = f"# {header}\n\n"
    md += "<!-- Managed by autolearn. Do not edit the structure. -->\n\n"
    for entry in entries:
        md += f"- {entry}\n"
    return md


def total_chars(entries: list[str]) -> int:
    return sum(len(e) for e in entries)


# @spec KS-MEM-006, KS-MEM-007, KS-MEM-008
def trim_entries(entries: list[str], max_chars: int) -> list[str]:
    total = total_chars(entries)
    if total <= max_chars:
        return entries
    while total_chars(entries) > max_chars and len(entries) > 1:
        entries = entries[1:]
    return entries


# @spec KS-MEM-005
def dedup(entries: list[str]) -> list[str]:
    seen = set()
    result = []
    for e in entries:
        normalized = e.lower().strip()
        if normalized not in seen:
            seen.add(normalized)
            result.append(e)
    return result


# @spec KS-MEM-002
def cmd_init(args):
    ensure_dirs()
    if not MEMORY_FILE.exists():
        write_md(MEMORY_FILE, "# Autolearn Memory\n\n<!-- Managed by autolearn. -->\n\n")
    if not USER_FILE.exists():
        write_md(USER_FILE, "# User Profile\n\n<!-- Managed by autolearn. -->\n\n")
    if not CONFIG_FILE.exists():
        write_md(CONFIG_FILE, "review_threshold: 5\nsession_review_on_idle: true\nmax_conversation_buffer: 50\ncurator_interval_days: 7\nstale_after_days: 30\narchive_after_days: 90\nescalation_threshold: 3\n")
    print(f"Initialized autolearn store at {DATA_HOME}")


# --- Memory Insight: registry-backed memory commands -----------------------
# The registry (memories.jsonl) is now the source of truth; these commands are
# thin wrappers over registry.py so the reviewer agent's existing CLI surface is
# preserved. See docs/designs/memory-insight/ (MI-REG-015/016).

def memory_registry():
    """MemoryRegistry for the active persona dir (set by set_persona)."""
    return registry.MemoryRegistry(ACTIVE_PERSONA_DIR)


def refresh_context():
    """Regenerate memory.context.md after a registry mutation (retention-only ranking)."""
    try:
        reg = memory_registry()
        md = composer.compose(reg, set())
        out = ACTIVE_PERSONA_DIR / "memory.context.md"
        tmp = out.with_suffix(out.suffix + f".{os.getpid()}.tmp")
        tmp.write_text(md, encoding="utf-8")
        os.replace(tmp, out)
    except Exception:
        pass  # context refresh must never break a memory write


# @spec MI-REG-015, MI-CMP-009
def cmd_memory_add(args):
    ensure_dirs()
    reg = memory_registry()
    rec = reg.add(args.content, "memory")
    refresh_context()
    print(f"Memory updated: {rec['text'][:80]}")


def cmd_memory_remove(args):
    reg = memory_registry()
    keyword = args.keyword.lower()
    matches = [r for r in reg.load_active() if keyword in r["text"].lower()]
    for r in matches:
        reg.remove(r["id"])
    refresh_context()
    print(f"Removed {len(matches)} entries ({len(reg.load_active())} remaining)")


def cmd_memory_list(args):
    reg = memory_registry()
    records = reg.load_active()
    if not records:
        print("Memory is empty.")
        return
    for i, r in enumerate(records, 1):
        print(f"  {i}. {r['text']}")
    total = len(reg.load())
    print(f"\nTotal: {len(records)} active entries (registry: {total} total incl. evicted)")


def cmd_memory_strengths(args):
    reg = memory_registry()
    records = [r for r in reg.load() if (r.get("reinforcements") or [])]
    if not records:
        print("No reinforcement data yet.")
        return
    records.sort(key=lambda r: len(r.get("reinforcements") or []), reverse=True)
    for r in records:
        count = len(r.get("reinforcements") or []) + 1
        snippet = r["text"][:77]
        print(f"  [{count}x] {snippet}  (first: {r.get('created_at', '?')}, last: {r.get('last_reinforced', '?')})")
    reinforced = sum(1 for r in records if (r.get("reinforcements") or []))
    print(f"\nTotal: {len(records)} entries tracked, {reinforced} reinforced")


def cmd_memory_strengthen(args):
    reg = memory_registry()
    keyword = args.keyword.lower()
    matches = [r for r in reg.load_active() if keyword in r["text"].lower()]
    if not matches:
        print(f"No memory entries matching '{args.keyword}'.")
        sys.exit(1)
    if len(matches) > 1:
        print(f"Multiple entries match '{args.keyword}', please be more specific:")
        for i, r in enumerate(matches, 1):
            print(f"  {i}. {r['text'][:100]}")
        sys.exit(1)
    rec = matches[0]
    reg.reinforce(rec["id"])
    refresh_context()
    new = reg.get(rec["id"])
    print(f"Strengthened: [{len(new['reinforcements']) + 1}x] {rec['text'][:80]}")


def cmd_memory_weaken(args):
    reg = memory_registry()
    keyword = args.keyword.lower()
    matches = [r for r in reg.load_active() if keyword in r["text"].lower()]
    if not matches:
        print(f"No memory entries matching '{args.keyword}'.")
        sys.exit(1)
    if len(matches) > 1:
        print(f"Multiple entries match '{args.keyword}', please be more specific:")
        for i, r in enumerate(matches, 1):
            print(f"  {i}. {r['text'][:100]}")
        sys.exit(1)
    rec = matches[0]
    reinf = list(rec.get("reinforcements") or [])
    if reinf:
        reinf.pop()  # drop most recent reinforcement
        rec["reinforcements"] = reinf
        rec["last_reinforced"] = reinf[-1] if reinf else rec.get("created_at")
        reg.update(rec)
        refresh_context()
        print(f"Weakened: [{len(reinf) + 1}x] {rec['text'][:80]}")
    else:
        print("No strength record for that entry.")


# --- Memory Insight: new top-level commands --------------------------------

# @spec MI-UI-001, MI-UI-002, MI-UI-003
def cmd_ui(args):
    inspector_server.serve(
        port=getattr(args, "port", 4321) or 4321,
        persona_dir=ACTIVE_PERSONA_DIR,
        open_browser=not bool(getattr(args, "no_browser", False)),
    )


# @spec MI-RTN-005
def cmd_retention_score(args):
    reg = memory_registry()
    summary = retention.score_all(reg)
    print(f"Scored {summary['total']} active memories:")
    for tier in ("hot", "warm", "cold", "evictable"):
        print(f"  {tier}: {summary[tier]}")


# @spec MI-RTN-007
def cmd_retention_evict(args):
    reg = memory_registry()
    result = retention.evict(reg, dry_run=bool(getattr(args, "dry_run", False)))
    label = "would evict" if result["dry_run"] else "evicted"
    print(f"{label} {result['evicted']} memories:")
    for c in result["candidates"]:
        print(f"  {c['id']} (score {c['score']})")


# @spec MI-CMP-001
def cmd_memory_compose(args):
    reg = memory_registry()
    ctx = composer.tokens(args.context) if getattr(args, "context", None) else set()
    md = composer.compose(reg, ctx)
    out = ACTIVE_PERSONA_DIR / "memory.context.md"
    tmp = out.with_suffix(out.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(md, encoding="utf-8")
    os.replace(tmp, out)
    n = max(0, md.count("\n- "))
    print(f"Wrote {out} ({n} entries, {len(md)} chars)")


# @spec MI-SFT-010
def cmd_topics_scan(args):
    reg = memory_registry()
    changed = shift.scan(reg, ACTIVE_PERSONA_DIR)
    if not changed:
        print("No new/updated candidates.")
    for c in changed:
        print(f"  [{c.get('status')}] {c.get('direction')} div={c.get('divergence')} "
              f"tokens={' '.join(c.get('tokens', [])[:6])}")


# @spec MI-SFT-011
def cmd_topics_candidates(args):
    pending = [c for c in shift.load_candidates(ACTIVE_PERSONA_DIR) if c.get("status") == "pending"]
    if not pending:
        print("No pending candidates.")
    for c in pending:
        print(f"  div={c.get('divergence')} sw={c.get('sw')} ema={c.get('ema')}")
        for u in c.get("utterances", [])[:2]:
            print(f"    \"{u}\"")


# @spec KS-MEM-004, KS-MEM-005, MI-REG-015  (user profile now lives in the registry, type="user")
def cmd_user_add(args):
    ensure_dirs()
    reg = memory_registry()
    reg.add(args.content, "user")
    refresh_context()
    print(f"User profile updated: {args.content[:80]}")


def cmd_user_remove(args):
    reg = memory_registry()
    keyword = args.keyword.lower()
    matches = [r for r in reg.load_active() if r.get("type") == "user" and keyword in r["text"].lower()]
    for r in matches:
        reg.remove(r["id"])
    refresh_context()
    print(f"Removed {len(matches)} entries ({len([r for r in reg.load_active() if r.get('type') == 'user'])} remaining)")


def cmd_user_list(args):
    reg = memory_registry()
    entries = [r for r in reg.load_active() if r.get("type") == "user"]
    if not entries:
        print("User profile is empty.")
        return
    for i, r in enumerate(entries, 1):
        print(f"  {i}. {r['text']}")


# @spec SM-SC-001, SM-SC-002, SM-SC-003, SM-SC-004, SM-SC-005
def cmd_skill_create(args):
    ensure_dirs()
    name = slugify(args.name)
    desc = args.description
    skill_dir = SKILLS_DIR / name
    skill_file = skill_dir / "SKILL.md"

    if skill_file.exists():
        print(f"Skill already exists: {name}")
        sys.exit(1)

    skill_dir.mkdir(parents=True, exist_ok=True)

    content = f"""---
name: {name}
description: |
  {desc}
created_by: autolearn
created_at: "{date.today().isoformat()}"
---

# {name.replace("-", " ").title()}

{desc}

## Instructions

TODO: Add specific instructions based on observed patterns.
"""
    skill_file.write_text(content)

    usage = load_usage()
    usage[name] = {
        "created_by": "autolearn",
        "created_at": date.today().isoformat(),
        "use_count": 0,
        "patch_count": 0,
        "last_activity_at": date.today().isoformat(),
        "state": "active",
    }
    save_usage(usage)

    # @spec WIKI-IMP-001, WIKI-SKL-001 — harness-written ledger + provenance
    if wiki_layer.enabled(ACTIVE_PERSONA_DIR):
        try:
            wiki_layer.ensure_scaffold(ACTIVE_PERSONA_DIR)
            wiki_layer.append_skill_impact(
                ACTIVE_PERSONA_DIR, action="create", skill=name,
                summary=desc[:300], outcome="accepted", trigger="review")
            purpose_patterns = [s.strip() for s in
                                getattr(args, "patterns", "").split(",") if s.strip()]
            wiki_layer.write_purpose(
                skill_dir, origin=desc[:300], patterns=purpose_patterns,
                evolution="created")
        except Exception as exc:
            print(f"(wiki ledger/provenance skipped: {exc})")

    link_path = AGENTS_SKILLS_DIR / name
    AGENTS_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    if not link_path.exists():
        link_path.symlink_to(skill_dir)

    print(f"Created skill: {name} at {skill_dir} (linked to {link_path})")


# @spec SM-SP-001, SM-SP-002, SM-SP-003, SM-SP-004, SM-SP-005
def cmd_skill_patch(args):
    name = slugify(args.name)
    section = args.section
    content = args.content
    skill_file = SKILLS_DIR / name / "SKILL.md"

    if not skill_file.exists():
        print(f"Skill not found: {name}")
        sys.exit(1)

    existing = skill_file.read_text()

    section_header = f"## {section}"
    if section_header in existing:
        lines = existing.splitlines()
        new_lines = []
        in_section = False
        inserted = False
        for line in lines:
            if line.strip() == section_header:
                in_section = True
                new_lines.append(line)
                continue
            if in_section and line.startswith("## ") and line.strip() != section_header:
                if not inserted:
                    new_lines.append(f"- {content}")
                    inserted = True
                in_section = False
                new_lines.append(line)
                continue
            if in_section:
                new_lines.append(line)
                continue
            new_lines.append(line)
        if in_section and not inserted:
            new_lines.append(f"- {content}")
        existing = "\n".join(new_lines)
    else:
        existing = existing.rstrip() + f"\n\n{section_header}\n\n- {content}\n"

    skill_file.write_text(existing)

    usage = load_usage()
    if name in usage:
        usage[name]["patch_count"] = usage[name].get("patch_count", 0) + 1
        usage[name]["last_activity_at"] = date.today().isoformat()
    save_usage(usage)

    # @spec WIKI-IMP-001, WIKI-SKL-002 — harness-written ledger + provenance append
    if wiki_layer.enabled(ACTIVE_PERSONA_DIR):
        try:
            wiki_layer.append_skill_impact(
                ACTIVE_PERSONA_DIR, action="patch", skill=name,
                summary=f"[{section}] {content[:250]}", outcome="accepted",
                trigger="review")
            wiki_layer.write_purpose(
                SKILLS_DIR / name, origin="", patterns=[],
                evolution=f"patched section '{section}'")
        except Exception as exc:
            print(f"(wiki ledger/provenance skipped: {exc})")

    print(f"Patched skill: {name} (section: {section})")


# @spec SM-SA-001, SM-SA-002, SM-SA-003, SM-SA-004, SM-SA-005
def cmd_skill_archive(args):
    name = slugify(args.name)
    skill_dir = SKILLS_DIR / name

    if not skill_dir.exists():
        print(f"Skill not found: {name}")
        sys.exit(1)

    dest = ARCHIVE_DIR / name
    if dest.exists():
        print(f"Skill already archived: {name}")
        sys.exit(1)

    skill_dir.rename(dest)

    link_path = AGENTS_SKILLS_DIR / name
    if link_path.is_symlink():
        link_path.unlink()

    usage = load_usage()
    if name in usage:
        usage[name]["state"] = "archived"
        usage[name]["archived_at"] = date.today().isoformat()
    save_usage(usage)

    # @spec WIKI-IMP-001 — harness-written ledger
    if wiki_layer.enabled(ACTIVE_PERSONA_DIR):
        try:
            wiki_layer.append_skill_impact(
                ACTIVE_PERSONA_DIR, action="archive", skill=name,
                summary="archived", outcome="archived", trigger="curator")
        except Exception as exc:
            print(f"(wiki ledger skipped: {exc})")

    print(f"Archived skill: {name}")


# @spec SM-SL-001, SM-SL-002
def cmd_skill_list(args):
    usage = load_usage()
    if not usage:
        print("No skills created yet.")
        return

    for name, meta in sorted(usage.items()):
        state = meta.get("state", "active")
        count = meta.get("use_count", 0)
        patches = meta.get("patch_count", 0)
        last = meta.get("last_activity_at", "unknown")
        print(f"  {name} [{state}] uses={count} patches={patches} last={last}")


# @spec SM-SU-001, SM-SU-002
def cmd_skill_usage(args):
    usage = load_usage()
    if not usage:
        print("No usage data.")
        return
    print(json.dumps(usage, indent=2))


# @spec SM-LC-001, SM-LC-002, SM-LC-003, SM-LC-004
# @spec SM-LC-005, SM-LC-006, SM-LC-007, SM-LC-017
# @spec SM-LC-008, SM-LC-009, SM-LC-011, SM-LC-012
def cmd_curator_run(args):
    config = load_config()
    stale_days = config.get("stale_after_days", 30)
    archive_days = config.get("archive_after_days", 90)
    escalation_threshold = config.get("escalation_threshold", 3)
    today = date.today()

    usage = load_usage()
    state = load_curator_state()

    transitions = {"stale": [], "archived": [], "active": []}

    for name, meta in list(usage.items()):
        if meta.get("state") == "archived":
            continue
        if meta.get("pinned"):
            continue
        if meta.get("created_by") != "autolearn":
            continue

        last_str = meta.get("last_activity_at")
        if not last_str:
            continue

        try:
            last_date = date.fromisoformat(last_str)
        except ValueError:
            continue

        days_inactive = (today - last_date).days
        current_state = meta.get("state", "active")

        if days_inactive >= archive_days and current_state != "archived":
            skill_dir = SKILLS_DIR / name
            if skill_dir.exists() and not (ARCHIVE_DIR / name).exists():
                skill_dir.rename(ARCHIVE_DIR / name)
            meta["state"] = "archived"
            meta["archived_at"] = today.isoformat()
            transitions["archived"].append(name)
        elif days_inactive >= stale_days and current_state == "active":
            meta["state"] = "stale"
            transitions["stale"].append(name)

    save_usage(usage)

    escalation_candidates = []
    # Memory Insight: escalation is driven by registry reinforcement counts
    # (memories.jsonl reinforcements[]), not a legacy strengths file.
    try:
        _reg_records = memory_registry().load_active()
    except Exception:
        _reg_records = []
    for rec in _reg_records:
        count = len(rec.get("reinforcements") or []) + 1
        if count >= escalation_threshold:
            escalation_candidates.append((rec.get("text", rec.get("id", "")), count))

    # @spec CP-INT-001 — Certified Procedures orchestration (best-effort).
    cp = {"indexed": None, "falsify": None, "usage_repaired": None}
    try:
        idx = outcomes.OutcomeIndex(OUTCOMES_DB, OPENCODE_DB)
        cp["indexed"] = idx.index()
        idx.close()
    except Exception as exc:  # never let CP break the curator
        cp["indexed"] = {"error": str(exc)}
    try:
        summary = falsify.verify_all(SKILLS_DIR, ACTIVE_PERSONA_DIR / falsify.VERDICTS_FILE)
        usage = load_usage()
        cons = falsify.apply_consequences(usage, summary["ledger"])
        save_usage(usage)
        cp["falsify"] = {k: summary[k] for k in ("pass", "fail", "inconclusive")}
        cp["falsify"]["demoted"] = len(cons["demoted"])
        # @spec WIKI-IMP-002 — demotions are harness-recorded verdicts
        if wiki_layer.enabled(ACTIVE_PERSONA_DIR) and cons["demoted"]:
            wiki_layer.ensure_scaffold(ACTIVE_PERSONA_DIR)
            for flag in cons["demoted"]:
                wiki_layer.append_skill_impact(
                    ACTIVE_PERSONA_DIR, action="demote", skill=flag.get("skill", "?"),
                    summary=f"failed falsification x{flag.get('fail_count', '?')}: "
                            f"{flag.get('evidence', '')[:200]}",
                    outcome="demoted", trigger="falsify")
    except Exception as exc:
        cp["falsify"] = {"error": str(exc)}
    try:
        cp["usage_repaired"] = repair_skill_use_counts()
    except Exception as exc:
        cp["usage_repaired"] = {"error": str(exc)}

    # @spec LH-PROP-001..003, LH-PRUNE-001 — long-horizon skill loop (best-effort)
    lh = {"scanned": None, "promoted": None, "pruned": None}
    try:
        lh["scanned"] = proposer.scan(ACTIVE_PERSONA_DIR)
        lh["verified"] = proposer.verify_pending(ACTIVE_PERSONA_DIR)
        lh["promoted"] = promote_proposals()
    except Exception as exc:
        lh["scanned"] = {"error": str(exc)}
    try:
        lh["pruned"] = prune_unused_skills(config)
        if lh["pruned"]["archived"]:
            transitions.setdefault("archived", []).extend(
                [n for n in lh["pruned"]["archived"] if n not in transitions["archived"]]
            )
    except Exception as exc:
        lh["pruned"] = {"error": str(exc)}

    # @spec WIKI-CUR-001 — wiki consolidation pass (best-effort, never fatal)
    try:
        lh["wiki_consolidated"] = consolidate_wiki(config)
    except Exception as exc:
        lh["wiki_consolidated"] = {"error": str(exc)}

    run_record = {
        "date": today.isoformat(),
        "transitions": transitions,
        "escalation_candidates": [
            {"entry": e, "strength": s} for e, s in escalation_candidates
        ],
        "certified_procedures": cp,
        "long_horizon": lh,
    }
    state["last_run"] = today.isoformat()
    state["runs"].append(run_record)
    save_curator_state(state)

    total_transitions = sum(len(v) for v in transitions.values())
    cp_fail = cp.get("falsify", {})
    cp_indexed = cp.get("indexed", {})
    has_cp_signal = (isinstance(cp_fail, dict) and cp_fail.get("demoted")) or \
                    (isinstance(cp_indexed, dict) and cp_indexed.get("gt_ge2"))
    if total_transitions == 0 and not escalation_candidates and not has_cp_signal:
        print("Curator run complete: no transitions needed, no escalation candidates.")
    else:
        print("Curator run complete:")
        for action, names in transitions.items():
            if names:
                print(f"  {action}: {', '.join(names)}")
        if escalation_candidates:
            print(f"  escalation candidates (strength >= {escalation_threshold}):")
            for snippet, count in sorted(escalation_candidates, key=lambda x: -x[1]):
                display = snippet[:80] + "..." if len(snippet) > 80 else snippet
                print(f"    [{count}x] {display}")
    # Certified Procedures summary (falsify demotions + reuse-ledger repair)
    if isinstance(cp_fail, dict) and "pass" in cp_fail:
        print(f"  procedures: {cp_fail.get('pass', 0)} pass, "
              f"{cp_fail.get('fail', 0)} fail, {cp_fail.get('inconclusive', 0)} inconclusive"
              + (f", {cp_fail['demoted']} demoted" if cp_fail.get('demoted') else ""))
    if isinstance(cp_indexed, dict) and cp_indexed.get("gt_ge2") is not None:
        print(f"  outcomes: indexed {cp_indexed.get('tool_parts', 0)} tool parts "
              f"({cp_indexed.get('gt_ge2', 0)} ground-truth)")
    if isinstance(cp.get("usage_repaired"), dict):
        ur = cp["usage_repaired"]
        if ur.get("updated"):
            print(f"  usage tracking: repaired use_count on {ur['updated']} skill(s)")
        if ur.get("added"):
            print(f"  usage tracking: newly tracking {ur['added']} manual skill(s)")
    # Long-horizon skill loop summary
    lh = run_record.get("long_horizon", {})
    if isinstance(lh.get("promoted"), dict) and (lh["promoted"].get("created") or lh["promoted"].get("skipped_already_existed")):
        print(f"  proposals: promoted {len(lh['promoted']['created'])} new skill(s)"
              + (f", {len(lh['promoted']['skipped_already_existed'])} already existed" if lh["promoted"]["skipped_already_existed"] else ""))
    if isinstance(lh.get("pruned"), dict) and lh["pruned"].get("archived"):
        print(f"  pruner: auto-archived {len(lh['pruned']['archived'])} never-used skill(s): "
              f"{', '.join(lh['pruned']['archived'])}")


# @spec SM-LC-010
def cmd_curator_status(args):
    state = load_curator_state()
    usage = load_usage()

    active = sum(1 for m in usage.values() if m.get("state") == "active")
    stale = sum(1 for m in usage.values() if m.get("state") == "stale")
    archived = sum(1 for m in usage.values() if m.get("state") == "archived")

    print(f"Skills: {active} active, {stale} stale, {archived} archived")
    print(f"Last curator run: {state.get('last_run', 'never')}")
    print(f"Total runs: {len(state.get('runs', []))}")


def get_search_conn() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(str(SEARCH_DB))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_search_schema(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS index_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS session_text_content(
            rowid INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL,
            message_id TEXT NOT NULL,
            role TEXT NOT NULL,
            text TEXT NOT NULL,
            project TEXT NOT NULL DEFAULT '',
            timestamp INTEGER NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS session_text USING fts5(
            session_id,
            message_id,
            role,
            text,
            project,
            timestamp,
            content=session_text_content,
            content_rowid=rowid
        );
        CREATE TABLE IF NOT EXISTS indexed_session(
            session_id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            project TEXT NOT NULL DEFAULT '',
            message_count INTEGER NOT NULL DEFAULT 0,
            time_created INTEGER NOT NULL,
            time_indexed INTEGER NOT NULL
        );
    """)


def get_opencode_conn() -> sqlite3.Connection | None:
    if not OPENCODE_DB.exists():
        return None
    conn = sqlite3.connect(f"file:{OPENCODE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def iso_from_unix_ms(unix_ms: int) -> str:
    dt = datetime.fromtimestamp(unix_ms / 1000)
    return dt.strftime("%Y-%m-%d %H:%M")


def cmd_search_init(args):
    full = getattr(args, "full", False)
    db_conn = get_opencode_conn()
    if not db_conn:
        print(f"OpenCode database not found at {OPENCODE_DB}")
        sys.exit(1)

    sconn = get_search_conn()
    init_search_schema(sconn)

    if full:
        sconn.execute("DELETE FROM session_text_content")
        sconn.execute("DELETE FROM indexed_session")
        sconn.execute("DELETE FROM index_state WHERE key = 'last_part_time'")
        sconn.execute("DELETE FROM index_state WHERE key = 'last_v2_message_time'")
        sconn.commit()
        print("Cleared existing index for full rebuild...")

    last_mark_row = sconn.execute(
        "SELECT value FROM index_state WHERE key = 'last_part_time'"
    ).fetchone()
    last_mark = int(last_mark_row["value"]) if last_mark_row else 0

    last_v2_mark_row = sconn.execute(
        "SELECT value FROM index_state WHERE key = 'last_v2_message_time'"
    ).fetchone()
    last_v2_mark = int(last_v2_mark_row["value"]) if last_v2_mark_row else 0

    session_cache: dict[str, dict] = {}

    def get_session(session_id: str) -> dict:
        if session_id in session_cache:
            return session_cache[session_id]
        row = None
        # v2 first: for ids present in both stores, session_v2 carries the
        # fresher title. The v2 table may be absent on v1-only installs.
        try:
            row = db_conn.execute(
                "SELECT id, title, project_id, time_created FROM session_v2 WHERE id = ?",
                [session_id],
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
        if row is None:
            row = db_conn.execute(
                "SELECT id, title, project_id, time_created FROM session WHERE id = ?",
                [session_id],
            ).fetchone()
        if row:
            project_row = db_conn.execute(
                "SELECT name FROM project WHERE id = ?", [row["project_id"]]
            ).fetchone()
            info = {
                "title": row["title"] or "Untitled",
                "project": (project_row["name"] or "") if project_row else "",
                "time_created": row["time_created"],
            }
        else:
            info = {"title": "Untitled", "project": "", "time_created": 0}
        session_cache[session_id] = info
        return info

    rows = db_conn.execute(
        """
        SELECT p.id AS part_id, p.message_id, p.session_id,
               p.time_created, p.data AS part_data, m.data AS msg_data
        FROM part p
        JOIN message m ON m.id = p.message_id
        WHERE p.time_created > ?
        ORDER BY p.time_created ASC
        """,
        [last_mark],
    )

    count = 0
    max_time = last_mark
    for row in rows:
        try:
            pdata = json.loads(row["part_data"])
        except (json.JSONDecodeError, TypeError):
            continue
        if pdata.get("type") != "text":
            continue
        text = pdata.get("text", "")
        if not text or not text.strip():
            continue

        try:
            mdata = json.loads(row["msg_data"])
        except (json.JSONDecodeError, TypeError):
            mdata = {}
        role = mdata.get("role", "unknown")

        session_id = row["session_id"]
        session = get_session(session_id)

        sconn.execute(
            "INSERT INTO session_text_content (session_id, message_id, role, text, project, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
            [session_id, row["message_id"], role, text, session.get("project", "") or "", row["time_created"]],
        )

        sconn.execute(
            "INSERT OR REPLACE INTO indexed_session (session_id, title, project, message_count, time_created, time_indexed) VALUES (?, ?, ?, COALESCE((SELECT message_count FROM indexed_session WHERE session_id = ?), 0) + 1, ?, ?)",
            [session_id, session["title"], session["project"], session_id, session["time_created"], int(datetime.now().timestamp() * 1000)],
        )

        if row["time_created"] > max_time:
            max_time = row["time_created"]
        count += 1

    if max_time > last_mark:
        sconn.execute(
            "INSERT OR REPLACE INTO index_state (key, value) VALUES ('last_part_time', ?)",
            [str(max_time)],
        )

    # OpenCode v2 (beta) sessions: message text lives in session_message.data
    # (JSON with a content array); metadata lives in session_v2. Tables are
    # absent on v1-only installs, in which case this loop is skipped.
    # Messages mirrored into the v1 `message` table are already indexed by
    # the v1 loop above — skip them here to avoid double-indexing.
    v2_count = 0
    v2_max_time = last_v2_mark
    has_v1_message_table = False
    try:
        has_v1_message_table = bool(
            db_conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'message'"
            ).fetchone()
        )
    except sqlite3.OperationalError:
        pass

    v2_sql = """
        SELECT sm.id AS msg_id, sm.session_id, sm.type AS role,
               sm.time_created, sm.data AS msg_data
        FROM session_message sm
        WHERE sm.time_created > ?
    """
    if has_v1_message_table:
        v2_sql += "  AND NOT EXISTS (SELECT 1 FROM message m WHERE m.id = sm.id)\n"
    v2_sql += "        ORDER BY sm.time_created ASC"
    try:
        v2_cursor = db_conn.execute(v2_sql, [last_v2_mark])
    except sqlite3.OperationalError:
        v2_cursor = None

    for row in (v2_cursor or []):
        if row["role"] not in ("user", "assistant"):
            continue
        try:
            mdata = json.loads(row["msg_data"])
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(mdata, dict):
            continue
        parts = mdata.get("content")
        if not isinstance(parts, list):
            continue

        session_id = row["session_id"]
        session = get_session(session_id)

        for i, part in enumerate(parts):
            if not isinstance(part, dict) or part.get("type") != "text":
                continue
            text = part.get("text", "")
            if not text or not text.strip():
                continue

            sconn.execute(
                "INSERT INTO session_text_content (session_id, message_id, role, text, project, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                [session_id, row["msg_id"], row["role"], text, session.get("project", "") or "", row["time_created"]],
            )
            sconn.execute(
                "INSERT OR REPLACE INTO indexed_session (session_id, title, project, message_count, time_created, time_indexed) VALUES (?, ?, ?, COALESCE((SELECT message_count FROM indexed_session WHERE session_id = ?), 0) + 1, ?, ?)",
                [session_id, session["title"], session["project"], session_id, session["time_created"], int(datetime.now().timestamp() * 1000)],
            )
            v2_count += 1

        if row["time_created"] > v2_max_time:
            v2_max_time = row["time_created"]

    if v2_max_time > last_v2_mark:
        sconn.execute(
            "INSERT OR REPLACE INTO index_state (key, value) VALUES ('last_v2_message_time', ?)",
            [str(v2_max_time)],
        )

    sconn.execute("INSERT INTO session_text(session_text) VALUES ('rebuild')")
    sconn.commit()
    sconn.close()
    db_conn.close()

    if count == 0 and v2_count == 0:
        print("Index is up to date. No new messages to index.")
    else:
        parts_summary = []
        if count:
            parts_summary.append(f"{count} v1 text parts (up to {iso_from_unix_ms(max_time)})")
        if v2_count:
            parts_summary.append(f"{v2_count} v2 text parts (up to {iso_from_unix_ms(v2_max_time)})")
        print(f"Indexed {' + '.join(parts_summary)}")


def cmd_search_query(args):
    sconn = get_search_conn()
    init_search_schema(sconn)

    terms = args.terms
    limit = getattr(args, "limit", 5) or 5
    context_size = getattr(args, "context", 2) or 2
    session_filter = getattr(args, "session", None)
    project_filter = getattr(args, "project", None)

    query_parts = []
    for word in terms.split():
        if word.isupper() or word.startswith('"') or word.startswith("("):
            query_parts.append(word)
        else:
            query_parts.append(f"{word}*")
    fts_query = " ".join(query_parts)

    where_clauses = []
    params: list[str] = []
    if session_filter:
        where_clauses.append("session_id = ?")
        params.append(session_filter)
    if project_filter:
        where_clauses.append("project = ?")
        params.append(project_filter)

    where_sql = ""
    if where_clauses:
        where_sql = " AND " + " AND ".join(where_clauses)

    try:
        hits = sconn.execute(
            f"""
            SELECT rowid, session_id, message_id, role, text, project, timestamp, rank
            FROM session_text
            WHERE session_text MATCH ?{where_sql}
            ORDER BY rank
            LIMIT ?
            """,
            [fts_query] + params + [limit],
        ).fetchall()
    except sqlite3.OperationalError as e:
        print(f"Search error: {e}")
        sconn.close()
        sys.exit(1)

    if not hits:
        print(f"No results for: {terms}")
        sconn.close()
        return

    session_ids = list({h["session_id"] for h in hits})
    sessions_info = {}
    for sid in session_ids:
        row = sconn.execute(
            "SELECT title, project FROM indexed_session WHERE session_id = ?", [sid]
        ).fetchone()
        if row:
            sessions_info[sid] = {"title": row["title"], "project": row["project"]}

    print(f'## Search Results: "{terms}"\n')

    seen_sessions: set[str] = set()
    for hit in hits:
        sid = hit["session_id"]
        ts = hit["timestamp"]
        role = hit["role"]
        text = hit["text"]
        rank = hit["rank"]
        info = sessions_info.get(sid, {"title": "Untitled", "project": ""})

        if sid not in seen_sessions:
            seen_sessions.add(sid)
            print(f'### Session: {info["title"]} ({sid}) — {iso_from_unix_ms(ts)}')
            print()

        role_label = "**User**" if role == "user" else "**Assistant**"
        print(f'{role_label} (match, rank {rank:.2f}):')
        print(f"> {text[:500]}")
        print()

        if context_size > 0:
            ctx_before = sconn.execute(
                """
                SELECT role, text, timestamp FROM session_text_content
                WHERE session_id = ? AND timestamp < ? AND rowid != ?
                ORDER BY timestamp DESC LIMIT ?
                """,
                [sid, ts, hit["rowid"], context_size],
            ).fetchall()
            for ctx in reversed(ctx_before):
                crole = "User" if ctx["role"] == "user" else "Assistant"
                print(f"**{crole}** (context):")
                print(f"> {ctx['text'][:300]}")
                print()

            ctx_after = sconn.execute(
                """
                SELECT role, text, timestamp FROM session_text_content
                WHERE session_id = ? AND timestamp > ? AND rowid != ?
                ORDER BY timestamp ASC LIMIT ?
                """,
                [sid, ts, hit["rowid"], context_size],
            ).fetchall()
            for ctx in ctx_after:
                crole = "User" if ctx["role"] == "user" else "Assistant"
                print(f"**{crole}** (context):")
                print(f"> {ctx['text'][:300]}")
                print()

        print("---\n")

    print(f"{len(hits)} results across {len(seen_sessions)} sessions")
    sconn.close()


def cmd_search_sessions(args):
    sconn = get_search_conn()
    init_search_schema(sconn)

    terms = args.terms
    rows = sconn.execute(
        """
        SELECT session_id, title, project, message_count, time_created
        FROM indexed_session
        WHERE title LIKE ? OR project LIKE ?
        ORDER BY time_created DESC
        LIMIT 20
        """,
        [f"%{terms}%", f"%{terms}%"],
    ).fetchall()

    if not rows:
        print(f"No sessions matching: {terms}")
        sconn.close()
        return

    print(f'## Sessions matching "{terms}"\n')
    for row in rows:
        ts = iso_from_unix_ms(row["time_created"]) if row["time_created"] else "unknown"
        print(f"- **{row['title']}** ({row['session_id']})")
        print(f"  Project: {row['project']} | Messages: {row['message_count']} | {ts}")
    print(f"\n{len(rows)} sessions")
    sconn.close()


def cmd_search_status(args):
    sconn = get_search_conn()
    init_search_schema(sconn)

    session_count = sconn.execute("SELECT COUNT(*) as c FROM indexed_session").fetchone()["c"]
    text_count = sconn.execute("SELECT COUNT(*) as c FROM session_text_content").fetchone()["c"]
    last_mark_row = sconn.execute(
        "SELECT value FROM index_state WHERE key = 'last_part_time'"
    ).fetchone()
    last_mark = int(last_mark_row["value"]) if last_mark_row else 0
    last_v2_row = sconn.execute(
        "SELECT value FROM index_state WHERE key = 'last_v2_message_time'"
    ).fetchone()
    last_v2_mark = int(last_v2_row["value"]) if last_v2_row else 0
    db_size = SEARCH_DB.stat().st_size if SEARCH_DB.exists() else 0

    print("Index status:")
    print(f"  Sessions indexed: {session_count}")
    print(f"  Text parts indexed: {text_count}")
    if last_mark:
        print(f"  Last indexed part (v1): {iso_from_unix_ms(last_mark)}")
    else:
        print(f"  Last indexed part (v1): never")
    if last_v2_mark:
        print(f"  Last indexed part (v2): {iso_from_unix_ms(last_v2_mark)}")
    else:
        print(f"  Last indexed part (v2): never")
    print(f"  Database size: {db_size / (1024 * 1024):.1f} MB")
    print(f"  Database path: {SEARCH_DB}")
    sconn.close()


def trim_jsonl(path: Path, max_lines: int = 1000):
    """Trim a JSONL file to the last max_lines entries."""
    try:
        content = path.read_text()
        lines = content.strip().split("\n") if content.strip() else []
        if len(lines) > max_lines:
            path.write_text("\n".join(lines[-max_lines:]) + "\n")
    except (OSError, IndexError):
        pass


# @spec KS-OBS-007
def cmd_log_review_complete(args):
    """Log a review-complete event to observations.jsonl.

    Creates an audit trail for detecting systematic capture gaps.
    """
    ensure_dirs()
    entry: dict = {
        "type": "review_complete",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if getattr(args, "nothing", False):
        entry["nothing_recorded"] = True
        entry["observations"] = 0
    else:
        entry["observations"] = getattr(args, "observations", 0) or 0
        entry["memory_updated"] = getattr(args, "memory_updated", False) or False
        entry["user_profile_updated"] = getattr(args, "user_profile_updated", False) or False
        entry["skills_created"] = getattr(args, "skills_created", 0) or 0
        entry["skills_patched"] = getattr(args, "skills_patched", 0) or 0
        topics_raw = getattr(args, "topics", "") or ""
        entry["topics"] = [t.strip() for t in topics_raw.split(",") if t.strip()]

    with open(OBSERVATIONS_FILE, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    trim_jsonl(OBSERVATIONS_FILE, 1000)

    if entry.get("nothing_recorded"):
        print("Logged: review_complete (nothing recorded)")
    else:
        print(f"Logged: review_complete ({entry['observations']} observations, topics: {entry.get('topics', [])})")


# ===========================================================================
# Persona management (Phase 3)
#
# Implements docs/designs/sync/persona-LLD.md + persona-EARS.md.
# ===========================================================================

DEFAULT_PERSONA_FILE = DATA_HOME / ".default_persona"


def get_default_persona() -> str:
    if DEFAULT_PERSONA_FILE.exists():
        name = DEFAULT_PERSONA_FILE.read_text().strip()
        if name:
            return name
    return "default"


def set_default_persona(name: str) -> None:
    DATA_HOME.mkdir(parents=True, exist_ok=True)
    DEFAULT_PERSONA_FILE.write_text(name + "\n")


# @spec SYNC-PER-001
def cmd_persona_create(args):
    name = slugify(args.name)
    desc = args.description
    persona_path = PERSONAS_DIR / name
    if persona_path.exists():
        print(f"Persona already exists: {name}")
        sys.exit(1)
    persona_path.mkdir(parents=True, exist_ok=True)
    (persona_path / "skills").mkdir(exist_ok=True)
    (persona_path / "skills" / ".archive").mkdir(exist_ok=True)
    registry = load_registry()
    registry[name] = {
        "uuid": str(uuid_mod.uuid4()),
        "description": desc,
        "sync_enabled": True,
        "created_at": date.today().isoformat(),
    }
    save_registry(registry)
    # Seed default files
    (persona_path / "memory.md").write_text(f"# Autolearn Memory ({name})\n\n<!-- Managed by autolearn. -->\n\n")
    (persona_path / "user-profile.md").write_text("# User Profile\n\n<!-- Managed by autolearn. -->\n\n")
    (persona_path / "config.yaml").write_text(
        f"review_threshold: 5\nsession_review_on_idle: true\nmax_conversation_buffer: 50\n"
        f"curator_interval_days: 7\nstale_after_days: 30\narchive_after_days: 90\nescalation_threshold: 3\n"
    )
    print(f"Created persona: {name} at {persona_path}")
    print(f"  UUID: {registry[name]['uuid']}")
    print(f"  Description: {desc}")


# @spec SYNC-PER-002
def cmd_persona_list(args):
    registry = load_registry()
    if not registry:
        print("No personas. Run `autolearn persona create <name> \"description\"`.")
        return
    machine_default = get_default_persona()
    for name, meta in sorted(registry.items()):
        uid = meta.get("uuid") or "unresolved"
        desc = meta.get("description", "")
        sync_en = meta.get("sync_enabled", True)
        archived = meta.get("archived", False)
        marker = " *" if name == machine_default else ""
        state = "archived" if archived else ("sync-on" if sync_en else "sync-off")
        uid_display = f"{uid[:12]}..." if len(uid) > 12 else uid
        print(f"  {name}{marker} [{state}] {uid_display} {desc}")


# @spec SYNC-PER-003
def cmd_persona_switch(args):
    name = args.name
    registry = load_registry()
    if name not in registry:
        print(f"Persona not found: {name}")
        sys.exit(1)
    if registry[name].get("archived"):
        print(f"Persona '{name}' is archived. Unarchive it first.")
        sys.exit(1)
    set_default_persona(name)
    print(f"Switched machine-wide default persona to: {name}")
    print(f"  Commands without --persona will now use '{name}'.")


# @spec SYNC-PER-004
def cmd_persona_archive(args):
    name = args.name
    registry = load_registry()
    if name not in registry:
        print(f"Persona not found: {name}")
        sys.exit(1)
    if name == "default":
        print("Cannot archive the 'default' persona.")
        sys.exit(1)
    registry[name]["archived"] = True
    registry[name]["sync_enabled"] = False
    registry[name]["archived_at"] = date.today().isoformat()
    save_registry(registry)
    # If this was the machine default, revert to 'default'
    if get_default_persona() == name:
        set_default_persona("default")
        print(f"  Machine default reverted to 'default'.")
    print(f"Archived persona: {name} (files kept locally, sync disabled)")


# @spec SYNC-PER-005
def cmd_persona_rename(args):
    old = slugify(args.old)
    new = slugify(args.new)
    registry = load_registry()
    if old not in registry:
        print(f"Persona not found: {old}")
        sys.exit(1)
    if new in registry:
        print(f"Persona already exists: {new}")
        sys.exit(1)
    old_path = PERSONAS_DIR / old
    new_path = PERSONAS_DIR / new
    if old_path.exists():
        old_path.rename(new_path)
    registry[new] = registry.pop(old)
    save_registry(registry)
    if get_default_persona() == old:
        set_default_persona(new)
    print(f"Renamed persona: {old} -> {new}")



# Each persona has its own UUID (stored in .persona_registry.json). The
# default persona's UUID is derived deterministically from the install
# salt so that Phase 1 encrypted blobs remain decryptable.
# ===========================================================================

LAST_SYNC_FILE = ACTIVE_PERSONA_DIR / ".last_sync.json"

SYNC_FILES = [
    "memories.jsonl",
    "topics.jsonl",
    "candidates.jsonl",
    "memory.context.md",
    "memory.md",          # legacy, kept so pre-Memory-Insight peers stay in sync
    "user-profile.md",    # legacy, kept so pre-Memory-Insight peers stay in sync
    "observations.jsonl",
    "config.yaml",
]

DEFAULT_SERVER_URL = "http://localhost:3001"


def load_sync_config() -> dict:
    if SYNC_CONFIG_FILE.exists():
        try:
            return yaml.safe_load(SYNC_CONFIG_FILE.read_text()) or {}
        except Exception:
            pass
    return {}


def save_sync_config(config: dict) -> None:
    ensure_dirs()
    SYNC_CONFIG_FILE.write_text(yaml.safe_dump(config, default_flow_style=False))


def load_last_sync() -> dict:
    if LAST_SYNC_FILE.exists():
        try:
            return json.loads(LAST_SYNC_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_last_sync(data: dict) -> None:
    ensure_dirs()
    LAST_SYNC_FILE.write_text(json.dumps(data, indent=2) + "\n")


# @spec SYNC-PROTO-002
def get_api_key() -> str:
    api_key = os.environ.get("AUTOLEARN_SYNC_API_KEY")
    if not api_key:
        raise SystemExit(
            "AUTOLEARN_SYNC_API_KEY environment variable is not set.\n"
            "Set it to the API key you chose when registering with the sync server."
        )
    return api_key


def get_server_url() -> str:
    config = load_sync_config()
    return config.get("server_url", DEFAULT_SERVER_URL)


# @spec SYNC-PROTO-001, SYNC-PROTO-014
def sync_request(method: str, path: str, *, body: dict | None = None,
                  timeout: float = 30.0, allow_register: bool = False) -> "requests.Response":
    api_key = get_api_key()
    url = f"{get_server_url().rstrip('/')}{path}"
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        return requests.request(method, url, json=body, headers=headers, timeout=timeout)
    except RequestsError as exc:
        # @spec SYNC-PROTO-014
        raise SystemExit(f"Sync server unreachable at {url}: {exc}") from exc


def ensure_registered(api_key: str) -> str:
    """Return the user_id for api_key, registering if necessary.

    Tries GET /sync/status first; on 401, attempts POST /sync/register.
    Other non-2xx codes abort with a clear error.
    """
    url = f"{get_server_url().rstrip('/')}/sync/status"
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        r = requests.get(url, headers=headers, timeout=10)
    except RequestsError as exc:
        raise SystemExit(f"Sync server unreachable at {url}: {exc}") from exc

    if r.status_code == 200:
        # Already registered. user_id = sha256(api_key).
        return sync_crypto.api_key_id(api_key)
    if r.status_code == 401:
        # Not registered yet (or wrong key). Try to register.
        reg_url = f"{get_server_url().rstrip('/')}/sync/register"
        try:
            reg = requests.post(reg_url, json={"api_key": api_key}, timeout=10)
        except RequestsError as exc:
            raise SystemExit(f"Sync server unreachable at {reg_url}: {exc}") from exc
        if reg.status_code == 201:
            return reg.json().get("user_id") or sync_crypto.api_key_id(api_key)
        if reg.status_code == 409:
            raise SystemExit(
                "API key already registered but authentication failed. "
                "Check that AUTOLEARN_SYNC_API_KEY matches the key you first registered with."
            )
        if reg.status_code == 400:
            raise SystemExit("API key too short (minimum 16 characters). Choose a longer key.")
        raise SystemExit(f"Registration failed (HTTP {reg.status_code}): {reg.text}")
    raise SystemExit(f"Unexpected response from server (HTTP {r.status_code}): {r.text}")


def get_master_key_or_prompt() -> bytes:
    """Return the master key from the keychain, prompting as a fallback.

    @spec SYNC-ENC-002, SYNC-ENC-003.
    """
    key = sync_crypto.load_master_key()
    if key is not None:
        return key
    # Fallback: no keychain entry. Prompt for the password and re-derive.
    if not SALT_FILE.exists():
        raise SystemExit(
            "No master key in keychain and no salt file. Run `autolearn sync login` first."
        )
    salt = sync_crypto.load_salt(SALT_FILE)
    password = getpass.getpass("Master password (keychain locked): ")
    return sync_crypto.derive_master_key(password, salt)


# @spec SYNC-ENC-001
def cmd_sync_login(args):
    """Derive master key from password, store in keychain, persist salt.

    Also ensures the API key is registered with the server.
    """
    ensure_dirs()

    # 1. Resolve server_url
    config = load_sync_config()
    if args.server_url:
        config["server_url"] = args.server_url
        save_sync_config(config)
    elif not config.get("server_url"):
        server = input(f"Sync server URL [{DEFAULT_SERVER_URL}]: ").strip()
        config["server_url"] = server or DEFAULT_SERVER_URL
        save_sync_config(config)

    # 2. Ensure API key is registered (also validates connectivity)
    api_key = get_api_key()
    user_id = ensure_registered(api_key)
    print(f"Authenticated to {get_server_url()} (user_id: {user_id[:12]}...)")

    # 3. Salt: load existing, or generate new on first login
    if SALT_FILE.exists():
        salt = sync_crypto.load_salt(SALT_FILE)
        print(f"Using existing salt ({SALT_FILE})")
    else:
        salt = sync_crypto.generate_salt()
        sync_crypto.save_salt(SALT_FILE, salt)
        print(f"Generated new salt ({SALT_FILE}, 32 bytes)")

    # 4. Prompt for password and derive master key
    password = getpass.getpass("Choose a master password: ")
    confirm = getpass.getpass("Confirm master password: ")
    if password != confirm:
        raise SystemExit("Passwords do not match.")
    if len(password) < 8:
        raise SystemExit("Password must be at least 8 characters.")

    master_key = sync_crypto.derive_master_key(password, salt)

    # 5. Store master key in keychain (or warn if unavailable)
    if sync_crypto.keychain_available():
        sync_crypto.store_master_key(master_key)
        print(f"Master key stored in OS keychain (service: {sync_crypto.KEYCHAIN_SERVICE})")
    else:
        # @spec SYNC-ENC-003
        print(
            "WARNING: OS keychain unavailable. You will be prompted for your "
            "password on every sync operation. Install a Secret Service provider "
            "(macOS Keychain works out of the box; Linux needs gnome-keyring or kwallet)."
        )

    persona_id = resolve_persona_uuid("default", salt)
    print(f"\nSync login complete.")
    print(f"  Server:    {get_server_url()}")
    print(f"  Persona:   default ({persona_id})")
    print(f"  Machine:   {sync_crypto.machine_id()}")
    print(f"\nRun `autolearn sync push` to upload your store.")


# @spec SYNC-ENC-011
def cmd_sync_logout(args):
    """Remove the master key from the OS keychain. Does not delete local data."""
    removed = sync_crypto.delete_master_key()
    if removed:
        print(f"Master key removed from OS keychain (service: {sync_crypto.KEYCHAIN_SERVICE})")
    else:
        print("No master key found in OS keychain (nothing to remove).")
    print("Local data is unchanged. Run `autolearn sync login` to re-add the key.")


# @spec SYNC-ENC-012
def cmd_sync_export_key(args):
    """Print the master key as a base58 recovery string."""
    master_key = sync_crypto.load_master_key()
    if master_key is None:
        if not SALT_FILE.exists():
            raise SystemExit(
                "No master key in keychain and no salt file. Run `autolearn sync login` first."
            )
        salt = sync_crypto.load_salt(SALT_FILE)
        password = getpass.getpass("Master password (keychain locked): ")
        master_key = sync_crypto.derive_master_key(password, salt)
    recovery = sync_crypto.encode_recovery_key(master_key)
    print("Recovery key (base58-encoded master key). Store this offline:")
    print()
    print(recovery)
    print()
    print("Anyone with this key can decrypt your synced data. Treat it like a password.")


def encrypt_local_file(master_key: bytes, persona_id: str, rel_path: str) -> dict | None:
    """Read a local file, encrypt it, return the push record, or None if missing."""
    file_path = ACTIVE_PERSONA_DIR / rel_path
    if not file_path.exists():
        return None
    plaintext = file_path.read_bytes()
    persona_key = sync_crypto.derive_persona_key(master_key, persona_id)
    file_key = sync_crypto.derive_file_key(persona_key, rel_path)
    record = sync_crypto.encrypt(file_key, plaintext)
    record["key"] = rel_path
    record["tag"] = ""  # cryptography's AESGCM embeds the tag in ciphertext
    record["updated_at"] = int(file_path.stat().st_mtime)
    return record


# @spec SYNC-PROTO-004, SYNC-PROTO-005, SYNC-PROTO-006
def cmd_sync_push(args):
    """Encrypt all local files and upload them to the sync server."""
    ensure_dirs()
    if not SALT_FILE.exists():
        raise SystemExit("Not logged in. Run `autolearn sync login` first.")
    salt = sync_crypto.load_salt(SALT_FILE)
    master_key = get_master_key_or_prompt()
    persona_id = resolve_persona_uuid(ACTIVE_PERSONA, salt)
    machine = sync_crypto.machine_id()

    files_payload = []
    for rel_path in SYNC_FILES:
        record = encrypt_local_file(master_key, persona_id, rel_path)
        if record is not None:
            files_payload.append(record)

    if not files_payload:
        print("No local files to sync.")
        return

    print(f"Pushing {len(files_payload)} file(s) to {get_server_url()}...")
    resp = sync_request("POST", "/sync/push", body={
        "persona_id": persona_id,
        "machine_id": machine,
        "files": files_payload,
    })

    if resp.status_code != 200:
        raise SystemExit(f"Push failed (HTTP {resp.status_code}): {resp.text}")

    data = resp.json()
    conflicts = data.get("conflicts", [])
    if conflicts:
        print(f"Pushed {len(files_payload)} file(s). {len(conflicts)} conflict(s):")
        for c in conflicts:
            print(f"  {c['key']}: remote is newer (updated_at {c['remote_updated_at']} from {c['remote_machine']})")
        print("Run `autolearn sync pull` to merge remote changes first.")
    else:
        print(f"Pushed {len(files_payload)} file(s). No conflicts.")

    save_last_sync({"timestamp": int(time.time()), "persona_id": persona_id, "direction": "push"})


def merge_observations(local_text: str, remote_text: str) -> str:
    """Union two observations.jsonl bodies, dedup by exact line, sort by timestamp.

    @spec SYNC-PROTO-009.
    """
    local_lines = {ln for ln in local_text.splitlines() if ln.strip()}
    remote_lines = {ln for ln in remote_text.splitlines() if ln.strip()}
    union = local_lines | remote_lines

    def sort_key(line: str) -> str:
        try:
            ts = json.loads(line).get("timestamp", "")
        except (json.JSONDecodeError, TypeError):
            return ""
        # Coerce to str so mixed numeric/string timestamps don't crash sorted().
        return str(ts) if ts is not None else ""

    sorted_lines = sorted(union, key=sort_key)
    return ("\n".join(sorted_lines) + "\n") if sorted_lines else ""


# @spec SYNC-PROTO-007, SYNC-PROTO-008, SYNC-PROTO-009, SYNC-PROTO-010
def cmd_sync_pull(args):
    """Download encrypted blobs, decrypt locally, and merge."""
    ensure_dirs()
    if not SALT_FILE.exists():
        raise SystemExit("Not logged in. Run `autolearn sync login` first.")
    salt = sync_crypto.load_salt(SALT_FILE)
    master_key = get_master_key_or_prompt()
    persona_id = resolve_persona_uuid(ACTIVE_PERSONA, salt)

    since = 0
    if not args.full:
        last = load_last_sync()
        since = last.get("timestamp", 0) - 1  # fudge by 1s to avoid boundary miss

    print(f"Pulling from {get_server_url()} (since={since})...")
    resp = sync_request("POST", "/sync/pull", body={
        "persona_id": persona_id,
        "since": since,
    })

    if resp.status_code != 200:
        raise SystemExit(f"Pull failed (HTTP {resp.status_code}): {resp.text}")

    files = resp.json().get("files", [])
    if not files:
        print("No new files to pull.")
        return

    persona_key = sync_crypto.derive_persona_key(master_key, persona_id)
    accepted = 0
    merged = 0
    skipped = 0
    tampered = 0

    for rec in files:
        rel_path = rec["key"]
        file_key = sync_crypto.derive_file_key(persona_key, rel_path)
        try:
            remote_plaintext = sync_crypto.decrypt(file_key, rec["nonce"], rec["ciphertext"])
        except sync_crypto.TamperError:
            # @spec SYNC-ENC-010
            print(f"  WARNING: tampering detected for {rel_path}, skipping")
            tampered += 1
            continue

        local_path = ACTIVE_PERSONA_DIR / rel_path
        if not local_path.exists():
            # @spec SYNC-PROTO-008
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(remote_plaintext)
            accepted += 1
            continue

        # Merge strategy depends on file type.
        if rel_path == "observations.jsonl":
            # @spec SYNC-PROTO-009
            local_text = local_path.read_text()
            remote_text = remote_plaintext.decode("utf-8")
            merged_text = merge_observations(local_text, remote_text)
            if merged_text != local_text:
                local_path.write_text(merged_text)
                merged += 1
            else:
                skipped += 1
        else:
            # @spec SYNC-PROTO-010
            local_mtime = int(local_path.stat().st_mtime)
            if rec["updated_at"] > local_mtime:
                local_path.write_bytes(remote_plaintext)
                accepted += 1
            else:
                skipped += 1

    print(f"Pull complete: {accepted} accepted, {merged} merged, {skipped} kept local, {tampered} tampered.")

    save_last_sync({"timestamp": int(time.time()), "persona_id": persona_id, "direction": "pull"})


# @spec SYNC-PROTO-015
def cmd_sync_status(args):
    """Show sync state across all personas on the server."""
    resp = sync_request("GET", "/sync/status")
    if resp.status_code != 200:
        raise SystemExit(f"Status failed (HTTP {resp.status_code}): {resp.text}")

    data = resp.json()
    personas = data.get("personas", [])
    if not personas:
        print("No synced personas yet. Run `autolearn sync push` to upload.")
        return

    for p in personas:
        ts = p.get("last_sync")
        ts_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "never"
        machines = ", ".join(p.get("machines", [])) or "(none)"
        print(f"  Persona {p['persona_id'][:8]}...: {p['files']} file(s), last sync {ts_str}, machines: {machines}")

    last = load_last_sync()
    if last.get("timestamp"):
        last_ts = datetime.fromtimestamp(last["timestamp"]).strftime("%Y-%m-%d %H:%M:%S")
        print(f"\nLocal last sync: {last_ts} ({last.get('direction', '?')})")
    print(f"Machine ID: {sync_crypto.machine_id()}")


def main():
    parser = argparse.ArgumentParser(
        prog="autolearn",
        description="Autolearn CLI - manages self-improvement store",
    )
    sub = parser.add_subparsers(dest="command")

    # --persona flag shared by all data-operating subcommands.
    persona_parent = argparse.ArgumentParser(add_help=False)
    persona_parent.add_argument("--persona", default=None,
                                help="Persona name (default: machine default or 'default')")

    sub.add_parser("init", help="Initialize the autolearn store")

    mem = sub.add_parser("memory", help="Manage persistent memory", parents=[persona_parent])
    mem_sub = mem.add_subparsers(dest="subcommand")
    mem_add = mem_sub.add_parser("add", help="Add a memory entry")
    mem_add.add_argument("content", help="The lesson to remember")
    mem_rm = mem_sub.add_parser("remove", help="Remove entries matching keyword")
    mem_rm.add_argument("keyword", help="Keyword to match")
    mem_sub.add_parser("list", help="List all memory entries")
    mem_sub.add_parser("strengths", help="Show reinforcement statistics")
    mem_strength = mem_sub.add_parser("strengthen", help="Reinforce an existing memory (agent semantic dedup)")
    mem_strength.add_argument("keyword", help="Keyword matching the entry to strengthen")
    mem_weaken = mem_sub.add_parser("weaken", help="Reduce reinforcement on a memory")
    mem_weaken.add_argument("keyword", help="Keyword matching the entry to weaken")
    mem_compose = mem_sub.add_parser("compose", help="Generate memory.context.md from the registry")
    mem_compose.add_argument("--context", default=None, help="Session context text for relevance ranking")

    usr = sub.add_parser("user", help="Manage user profile", parents=[persona_parent])
    usr_sub = usr.add_subparsers(dest="subcommand")
    usr_add = usr_sub.add_parser("add", help="Add a preference")
    usr_add.add_argument("content", help="The preference to record")
    usr_rm = usr_sub.add_parser("remove", help="Remove entries matching keyword")
    usr_rm.add_argument("keyword", help="Keyword to match")
    usr_sub.add_parser("list", help="List all user profile entries")

    sk = sub.add_parser("skill", help="Manage agent-created skills", parents=[persona_parent])
    sk_sub = sk.add_subparsers(dest="subcommand")
    sk_create = sk_sub.add_parser("create", help="Create a new skill")
    sk_create.add_argument("name", help="Skill name")
    sk_create.add_argument("description", help="Skill description")
    sk_create.add_argument("--patterns", default="", help="Comma-separated wiki pattern slugs motivating this skill (recorded in PURPOSE.md)")
    sk_patch = sk_sub.add_parser("patch", help="Patch an existing skill")
    sk_patch.add_argument("name", help="Skill name")
    sk_patch.add_argument("section", help="Section to patch")
    sk_patch.add_argument("content", help="Content to add")
    sk_archive = sk_sub.add_parser("archive", help="Archive a skill")
    sk_archive.add_argument("name", help="Skill name")
    sk_sub.add_parser("list", help="List all agent-created skills")
    sk_sub.add_parser("usage", help="Show usage telemetry")

    cur = sub.add_parser("curator", help="Skill lifecycle management", parents=[persona_parent])
    cur_sub = cur.add_subparsers(dest="subcommand")
    cur_sub.add_parser("run", help="Run curator (stale/archive transitions)")
    cur_sub.add_parser("status", help="Show curator state")

    srch = sub.add_parser("search", help="FTS5 full-text search over past sessions", parents=[persona_parent])
    srch_sub = srch.add_subparsers(dest="subcommand")
    srch_init = srch_sub.add_parser("init", help="Build or update the FTS5 search index")
    srch_init.add_argument("--full", action="store_true", help="Full rebuild instead of incremental")
    srch_query = srch_sub.add_parser("query", help="Search for messages matching terms")
    srch_query.add_argument("terms", help="FTS5 search query")
    srch_query.add_argument("--limit", type=int, default=5, help="Max results (default 5)")
    srch_query.add_argument("--context", type=int, default=2, help="Surrounding messages per hit (default 2)")
    srch_query.add_argument("--session", help="Restrict to a specific session ID")
    srch_query.add_argument("--project", help="Restrict to a specific project")
    srch_sessions = srch_sub.add_parser("sessions", help="Search session titles")
    srch_sessions.add_argument("terms", help="Search terms for session titles")
    srch_sub.add_parser("status", help="Show search index status")

    log = sub.add_parser("log", help="Log structured events to observations.jsonl", parents=[persona_parent])
    log_sub = log.add_subparsers(dest="subcommand")
    log_rc = log_sub.add_parser("review-complete", help="Log review completion outcome")
    log_rc.add_argument("--observations", type=int, default=0, help="Number of observations recorded")
    log_rc.add_argument("--memory-updated", action="store_true", help="Memory was updated")
    log_rc.add_argument("--user-profile-updated", action="store_true", help="User profile was updated")
    log_rc.add_argument("--skills-created", type=int, default=0, help="Skills created count")
    log_rc.add_argument("--skills-patched", type=int, default=0, help="Skills patched count")
    log_rc.add_argument("--topics", default="", help="Comma-separated topics in the conversation")
    log_rc.add_argument("--nothing", action="store_true", help="Nothing was recorded")

    # Wiki layer: pattern store + evolution log + skill-impact ledger.
    # Spec: docs/designs/wiki-layer/LLD.md
    wl = sub.add_parser("wiki", help="Persistent pattern notebook (wiki layer)", parents=[persona_parent])
    wl_sub = wl.add_subparsers(dest="subcommand")
    wl_sub.add_parser("init", help="Create wiki/ scaffold + backfill PURPOSE.md")
    wl_sub.add_parser("list", help="List pattern pages")
    wl_show = wl_sub.add_parser("show", help="Print one pattern page")
    wl_show.add_argument("slug", help="Pattern slug")
    wl_read = wl_sub.add_parser("read", help="Lexically search the pattern index")
    wl_read.add_argument("terms", help="Key terms to match against index one-liners")
    wl_sub.add_parser("compose", help="Render wiki/context.md (reviewer-facing view)")
    wl_sub.add_parser("backfill-purposes", help="Write PURPOSE.md for existing persona-local skills")

    logs_w = sub.add_parser("logs", help="Wiki evolution log", parents=[persona_parent])
    logs_sub = logs_w.add_subparsers(dest="subcommand")
    logs_append = logs_sub.add_parser("append", help="Append a review summary to wiki/logs.md")
    logs_append.add_argument("summary", help="One-line summary of this review's findings")

    imp = sub.add_parser("impact", help="Skill-impact ledger (harness-written; debug append)", parents=[persona_parent])
    imp_sub = imp.add_subparsers(dest="subcommand")
    imp_append = imp_sub.add_parser("append", help="Append a ledger entry (debug/testing)")
    imp_append.add_argument("--action", required=True, choices=["create", "patch", "demote", "archive", "promote"])
    imp_append.add_argument("--skill", required=True)
    imp_append.add_argument("--summary", required=True)
    imp_append.add_argument("--outcome", required=True, choices=["accepted", "rejected", "demoted", "archived"])
    imp_append.add_argument("--trigger", required=True, choices=["review", "falsify", "proposer", "curator"])

    sync = sub.add_parser("sync", help="Cross-machine sync (E2E-encrypted)", parents=[persona_parent])
    sync_sub = sync.add_subparsers(dest="subcommand")
    sync_login = sync_sub.add_parser("login", help="Derive master key and store in keychain")
    sync_login.add_argument("--server-url", help="Sync server URL (default: http://localhost:3001)")
    sync_sub.add_parser("logout", help="Remove master key from keychain")
    sync_sub.add_parser("export-key", help="Print base58 recovery key for offline backup")
    sync_sub.add_parser("push", help="Encrypt and upload all local files")
    sync_pull = sync_sub.add_parser("pull", help="Download, decrypt, and merge remote files")
    sync_pull.add_argument("--full", action="store_true", help="Pull all files, ignoring last-sync timestamp")
    sync_sub.add_parser("status", help="Show sync state on the server")

    # @spec SYNC-PER-001 through SYNC-PER-005
    per = sub.add_parser("persona", help="Manage knowledge personas")
    per_sub = per.add_subparsers(dest="subcommand")
    per_create = per_sub.add_parser("create", help="Create a new persona")
    per_create.add_argument("name", help="Persona name")
    per_create.add_argument("description", help="Persona description")
    per_sub.add_parser("list", help="List all personas")
    per_switch = per_sub.add_parser("switch", help="Set machine-wide default persona")
    per_switch.add_argument("name", help="Persona name")
    per_archive = per_sub.add_parser("archive", help="Archive a persona")
    per_archive.add_argument("name", help="Persona name")
    per_rename = per_sub.add_parser("rename", help="Rename a persona")
    per_rename.add_argument("old", help="Old name")
    per_rename.add_argument("new", help="New name")

    # --- Memory Insight commands ---
    ui_p = sub.add_parser("ui", help="Launch the inspector UI", parents=[persona_parent])
    ui_p.add_argument("--port", type=int, default=4321, help="Port (default 4321)")
    ui_p.add_argument("--no-browser", action="store_true", help="Do not open a browser")

    rtn = sub.add_parser("retention", help="Ebbinghaus retention scoring & eviction", parents=[persona_parent])
    rtn_sub = rtn.add_subparsers(dest="subcommand")
    rtn_sub.add_parser("score", help="Recompute retention scores & tiers")
    rtn_ev = rtn_sub.add_parser("evict", help="Evict memories past the cold grace period")
    rtn_ev.add_argument("--dry-run", action="store_true", help="Report without mutating")

    topo = sub.add_parser("topics", help="Recurring-preference (shift) detector", parents=[persona_parent])
    topo_sub = topo.add_subparsers(dest="subcommand")
    topo_sub.add_parser("scan", help="Scan recent sessions for rising/falling topics")
    topo_sub.add_parser("candidates", help="List pending candidate preferences")

    # --- Certified Procedures commands ---
    oc = sub.add_parser("outcomes", help="Index opencode.db tool-call outcomes (Certified Procedures spine)", parents=[persona_parent])
    oc_sub = oc.add_subparsers(dest="subcommand")
    oc_init = oc_sub.add_parser("init", help="Build/update the outcome index from opencode.db")
    oc_init.add_argument("--full", action="store_true", help="Full rebuild")
    oc_sub.add_parser("status", help="Show outcome index size and coverage")

    fal = sub.add_parser("falsify", help="Falsify skills against their claims (Loop 1)", parents=[persona_parent])
    fal_sub = fal.add_subparsers(dest="subcommand")
    fal_run = fal_sub.add_parser("run", help="Verify skills and apply consequences")
    fal_run.add_argument("--id", default=None, help="Verify a single skill by name (searches persona + ~/.agents/skills)")
    fal_run.add_argument("--all", action="store_true", help="Also scan ~/.agents/skills/ (installed skills with claims)")
    fal_run.add_argument("--dry-run", action="store_true", help="Report without mutating")
    fal_sub.add_parser("verdicts", help="Print the per-skill verdict ledger")

    sho = sub.add_parser("shortcuts", help="Detect roundabout workflows + golden paths (Loop 2)", parents=[persona_parent])
    sho_sub = sho.add_subparsers(dest="subcommand")
    sho_det = sho_sub.add_parser("detect", help="Scan recent sessions for roundabout paths")
    sho_det.add_argument("--dry-run", action="store_true", help="Report without saving candidates")
    sho_sub.add_parser("list", help="List staged golden-path candidates")

    # --- Long-horizon skill loop commands ---
    pro = sub.add_parser("proposals", help="Cross-session skill proposals + recurrence gate", parents=[persona_parent])
    pro_sub = pro.add_subparsers(dest="subcommand")
    pro_sub.add_parser("list", help="List pending/promoted/dismissed proposals")
    pro_sub.add_parser("scan", help="Scan recent sessions; stage + verify proposals")
    pro_rec = pro_sub.add_parser("recurrence", help="Hard-gate check: does a request recur across sessions?")
    pro_rec.add_argument("text", help="The candidate pattern / user request text")
    pro_conf = pro_sub.add_parser("confirm", help="Manually force-promote a proposal")
    pro_conf.add_argument("id", help="Proposal id")
    pro_dis = pro_sub.add_parser("dismiss", help="Dismiss a proposal")
    pro_dis.add_argument("id", help="Proposal id")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)

    commands = {
        "init": cmd_init,
        "memory": {
            "add": cmd_memory_add,
            "remove": cmd_memory_remove,
            "list": cmd_memory_list,
            "strengths": cmd_memory_strengths,
            "strengthen": cmd_memory_strengthen,
            "weaken": cmd_memory_weaken,
            "compose": cmd_memory_compose,
        },
        "user": {
            "add": cmd_user_add,
            "remove": cmd_user_remove,
            "list": cmd_user_list,
        },
        "skill": {
            "create": cmd_skill_create,
            "patch": cmd_skill_patch,
            "archive": cmd_skill_archive,
            "list": cmd_skill_list,
            "usage": cmd_skill_usage,
        },
        "curator": {
            "run": cmd_curator_run,
            "status": cmd_curator_status,
        },
        "search": {
            "init": cmd_search_init,
            "query": cmd_search_query,
            "sessions": cmd_search_sessions,
            "status": cmd_search_status,
        },
        "log": {
            "review-complete": cmd_log_review_complete,
        },
        "sync": {
            "login": cmd_sync_login,
            "logout": cmd_sync_logout,
            "export-key": cmd_sync_export_key,
            "push": cmd_sync_push,
            "pull": cmd_sync_pull,
            "status": cmd_sync_status,
        },
        "persona": {
            "create": cmd_persona_create,
            "list": cmd_persona_list,
            "switch": cmd_persona_switch,
            "archive": cmd_persona_archive,
            "rename": cmd_persona_rename,
        },
        "ui": cmd_ui,
        "retention": {
            "score": cmd_retention_score,
            "evict": cmd_retention_evict,
        },
        "topics": {
            "scan": cmd_topics_scan,
            "candidates": cmd_topics_candidates,
        },
        "outcomes": {
            "init": outcomes.cmd_outcomes_init,
            "status": outcomes.cmd_outcomes_status,
        },
        "falsify": {
            "run": falsify.cmd_falsify_run,
            "verdicts": falsify.cmd_falsify_verdicts,
        },
        "shortcuts": {
            "detect": shortcuts.cmd_shortcuts_detect,
            "list": shortcuts.cmd_shortcuts_list,
        },
        "proposals": {
            "list": proposer.cmd_proposals_list,
            "scan": cmd_proposals_scan,
            "recurrence": proposer.cmd_proposals_recurrence,
            "confirm": proposer.cmd_proposals_confirm,
            "dismiss": proposer.cmd_proposals_dismiss,
        },
        "wiki": {
            "init": wiki_layer.cmd_wiki_init,
            "list": wiki_layer.cmd_wiki_list,
            "show": wiki_layer.cmd_wiki_show,
            "read": wiki_layer.cmd_wiki_read,
            "compose": wiki_layer.cmd_wiki_compose,
            "backfill-purposes": wiki_layer.cmd_wiki_backfill,
        },
        "logs": {
            "append": wiki_layer.cmd_logs_append,
        },
        "impact": {
            "append": wiki_layer.cmd_impact_append,
        },
    }

    # @spec SYNC-PER-006, SYNC-PER-015, SYNC-PER-016
    persona_name = getattr(args, "persona", None)
    if persona_name is None:
        persona_name = get_default_persona()
    set_persona(persona_name)

    cmd_map = commands.get(args.command)
    if isinstance(cmd_map, dict):
        subcmd = getattr(args, "subcommand", None)
        if not subcmd:
            print(f"Usage: autolearn {args.command} <subcommand>")
            sys.exit(1)
        fn = cmd_map.get(subcmd)
        if fn:
            ensure_dirs()
            fn(args)
        else:
            print(f"Unknown subcommand: {args.command} {subcmd}")
            sys.exit(1)
    elif callable(cmd_map):
        ensure_dirs()
        cmd_map(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
