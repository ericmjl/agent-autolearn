# /// script
# dependencies = [
#     "pytest",
#     "pyyaml",
#     "python-slugify",
#     "cryptography",
#     "keyring",
#     "keyrings.alt",
#     "requests",
# ]
# ///
"""Tests for repair_skill_use_counts() and the _scan_skill_dirs_for_repair() helper.

Covers SM-LC-013 through SM-LC-017 (usage tracking repair extension that brings
previously-untracked, user-installed skills under telemetry coverage without
subjecting them to lifecycle management).
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

# Make autolearn.py + outcomes.py importable when run via uv run / pytest.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import autolearn
import outcomes


# ---------------------------------------------------------------------------
# Helpers — synthetic opencode.db + isolated skill discovery dirs
# ---------------------------------------------------------------------------

def _make_opencode_db(path: Path, skill_loads: list[tuple[str, int]]) -> None:
    """Build a minimal opencode.db with `skill_loads` = [(name, time_created_ms), ...]."""
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE `part` (
            `id` text PRIMARY KEY,
            `message_id` text NOT NULL,
            `session_id` text NOT NULL,
            `time_created` integer NOT NULL,
            `time_updated` integer NOT NULL,
            `data` text NOT NULL
        );
        """
    )
    for i, (skill_name, t) in enumerate(skill_loads):
        conn.execute(
            "INSERT INTO part(id, message_id, session_id, time_created, time_updated, data) "
            "VALUES (?,?,?,?,?,?)",
            [
                f"p{i}", f"m{i}", "s", t, t,
                json.dumps({
                    "type": "tool", "tool": "skill",
                    "state": {"status": "completed", "input": {"name": skill_name}, "output": ""},
                }),
            ],
        )
    conn.commit()
    conn.close()


def _make_skill_on_disk(base: Path, name: str, contents: str = "# skill\n") -> Path:
    """Create a synthetic SKILL.md at base/name/SKILL.md and return its path."""
    skill_dir = base / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(contents)
    return skill_md


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    """Isolate persona dir, opencode.db path, and skill discovery dirs.

    Patches ALL the module-level paths in autolearn.py that the repair code
    touches, including DATA_HOME / PERSONAS_DIR / REGISTRY_FILE, so the test
    can never pollute the real ~/.autolearn/ even on a fresh CI runner where
    ensure_dirs() would otherwise mkdir the real directory.
    """
    home = tmp_path / "autolearn_home"
    persona = home / "personas" / "default"
    skills_dir = persona / "skills"
    skills_dir.mkdir(parents=True)

    agents_skills = tmp_path / "agents_skills"
    agents_skills.mkdir()
    config_skills = tmp_path / "config_skills"
    config_skills.mkdir()

    opencode_db = tmp_path / "opencode.db"

    # Patch every related module-level path. set_persona() does this in
    # production; we replicate the relevant subset here.
    monkeypatch.setattr(autolearn, "DATA_HOME", home)
    monkeypatch.setattr(autolearn, "PERSONAS_DIR", home / "personas")
    monkeypatch.setattr(autolearn, "REGISTRY_FILE", home / ".persona_registry.json")
    monkeypatch.setattr(autolearn, "ACTIVE_PERSONA_DIR", persona)
    monkeypatch.setattr(autolearn, "SKILLS_DIR", skills_dir)
    monkeypatch.setattr(autolearn, "ARCHIVE_DIR", skills_dir / ".archive")
    monkeypatch.setattr(autolearn, "USAGE_FILE", skills_dir / ".usage.json")
    monkeypatch.setattr(autolearn, "CURATOR_STATE_FILE", persona / ".curator_state.json")
    monkeypatch.setattr(autolearn, "OUTCOMES_DB", persona / "outcomes.db")
    monkeypatch.setattr(autolearn, "OPENCODE_DB", opencode_db)
    # Pre-create the registry file so init_registry() is a no-op even if a
    # future code path calls ensure_dirs() before repair.
    (home / ".persona_registry.json").write_text('{"default": "default"}')

    # Discovery dirs via env var so _skill_discovery_dirs() picks them up.
    monkeypatch.setenv(
        "AUTOLEARN_SKILL_DISCOVERY",
        f"{agents_skills}{os.pathsep}{config_skills}",
    )

    return type("NS", (), {
        "home": home,
        "persona": persona,
        "skills_dir": skills_dir,
        "usage_file": skills_dir / ".usage.json",
        "agents_skills": agents_skills,
        "config_skills": config_skills,
        "opencode_db": opencode_db,
    })()


def _index_outcomes(ns) -> None:
    """Build the outcome index from the synthetic opencode.db."""
    idx = outcomes.OutcomeIndex(ns.persona / "outcomes.db", ns.opencode_db)
    idx.index()
    idx.close()


# ---------------------------------------------------------------------------
# Phase 1 — update existing entries
# ---------------------------------------------------------------------------

# @spec SM-LC-016
def test_repair_updates_use_count_on_existing_entry(isolated_env):
    ns = isolated_env
    _make_opencode_db(ns.opencode_db, [("atomic-commits", 1_000), ("atomic-commits", 2_000)])
    _index_outcomes(ns)

    ns.usage_file.write_text(json.dumps({
        "atomic-commits": {
            "created_by": "autolearn",
            "created_at": "2026-01-01",
            "use_count": 0,
            "patch_count": 0,
            "last_activity_at": "2026-01-01",
            "state": "active",
            "pinned": False,
        }
    }))

    summary = autolearn.repair_skill_use_counts()
    assert summary["updated"] == 1
    assert summary["added"] == 0

    usage = json.loads(ns.usage_file.read_text())
    assert usage["atomic-commits"]["use_count"] == 2
    # SM-LC-016: created_by unchanged.
    assert usage["atomic-commits"]["created_by"] == "autolearn"


def test_repair_refreshes_last_activity_at_when_newer_load_observed(isolated_env):
    ns = isolated_env
    # Use a timestamp well after the existing last_activity_at; compute the
    # expected local-date via the same datetime.fromtimestamp() the
    # implementation uses, so the test is TZ-agnostic.
    load_ms = 1_782_748_801_000
    expected_iso = __import__("datetime").datetime.fromtimestamp(load_ms / 1000).strftime("%Y-%m-%d")
    _make_opencode_db(ns.opencode_db, [("foo", load_ms)])
    _index_outcomes(ns)

    ns.usage_file.write_text(json.dumps({
        "foo": {
            "created_by": "autolearn", "created_at": "2026-01-01",
            "use_count": 0, "patch_count": 0,
            "last_activity_at": "2026-01-01",
            "state": "active", "pinned": False,
        }
    }))

    summary = autolearn.repair_skill_use_counts()
    assert summary["updated"] == 1

    usage = json.loads(ns.usage_file.read_text())
    assert usage["foo"]["last_activity_at"] == expected_iso


def test_repair_leaves_unchanged_entries_unwritten(isolated_env):
    ns = isolated_env
    _make_opencode_db(ns.opencode_db, [("foo", 1_000)])
    _index_outcomes(ns)

    initial = {
        "foo": {
            "created_by": "autolearn", "created_at": "2026-01-01",
            "use_count": 1, "patch_count": 0,
            "last_activity_at": "2026-01-01",  # 1_000ms == 1970-01-01, older not newer
            "state": "active", "pinned": False,
        }
    }
    ns.usage_file.write_text(json.dumps(initial))

    summary = autolearn.repair_skill_use_counts()
    # use_count already correct (1), last_activity_at already newer than the load (1_000ms = 1970-01-01).
    assert summary["updated"] == 0
    assert summary["added"] == 0


# ---------------------------------------------------------------------------
# Phase 2 — add tracked-manual entries (SM-LC-013, SM-LC-014, SM-LC-015)
# ---------------------------------------------------------------------------

# @spec SM-LC-013, SM-LC-014
def test_repair_adds_tracked_manual_entry_for_loaded_untracked_skill(isolated_env):
    ns = isolated_env
    _make_opencode_db(ns.opencode_db, [("atomic-commits", 1_000), ("atomic-commits", 2_000)])
    _index_outcomes(ns)

    # Skill exists on disk but not in .usage.json.
    _make_skill_on_disk(ns.agents_skills, "atomic-commits")

    ns.usage_file.write_text(json.dumps({}))

    summary = autolearn.repair_skill_use_counts()
    assert summary["added"] == 1
    assert summary["updated"] == 0

    usage = json.loads(ns.usage_file.read_text())
    entry = usage["atomic-commits"]
    assert entry["created_by"] == "tracked-manual"
    assert entry["use_count"] == 2
    assert entry["state"] == "active"
    assert entry["pinned"] is False
    assert entry["patch_count"] == 0
    # last_activity_at derived from the most recent load (2_000ms in local time).
    expected_last = __import__("datetime").datetime.fromtimestamp(2.0).strftime("%Y-%m-%d")
    assert entry["last_activity_at"] == expected_last


# @spec SM-LC-014 — never loaded -> not added (no signal).
def test_repair_does_not_add_skill_with_zero_loads(isolated_env):
    ns = isolated_env
    # No skill loads at all.
    _make_opencode_db(ns.opencode_db, [])
    _index_outcomes(ns)

    _make_skill_on_disk(ns.agents_skills, "never-loaded")

    ns.usage_file.write_text(json.dumps({}))

    summary = autolearn.repair_skill_use_counts()
    assert summary["added"] == 0

    usage = json.loads(ns.usage_file.read_text())
    assert "never-loaded" not in usage


# @spec SM-LC-015 — archive dirs must be skipped.
@pytest.mark.parametrize("archive_name", [".archive", ".archive-manual", ".archive-old"])
def test_repair_skills_in_archive_dirs_not_resurrected(isolated_env, archive_name):
    ns = isolated_env
    _make_opencode_db(ns.opencode_db, [("old-skill", 1_000)])
    _index_outcomes(ns)

    archive_dir = ns.agents_skills / archive_name
    _make_skill_on_disk(archive_dir, "old-skill")

    ns.usage_file.write_text(json.dumps({}))

    summary = autolearn.repair_skill_use_counts()
    assert summary["added"] == 0

    usage = json.loads(ns.usage_file.read_text())
    assert "old-skill" not in usage


def test_repair_does_not_touch_existing_created_by(isolated_env):
    """Even if the skill is also on disk, an existing entry's created_by stays."""
    ns = isolated_env
    _make_opencode_db(ns.opencode_db, [("foo", 1_000)])
    _index_outcomes(ns)

    _make_skill_on_disk(ns.agents_skills, "foo")

    ns.usage_file.write_text(json.dumps({
        "foo": {
            "created_by": "user",  # legacy value, must be preserved
            "created_at": "2026-01-01",
            "use_count": 0,
            "patch_count": 0,
            "last_activity_at": "2026-01-01",
            "state": "active",
            "pinned": False,
        }
    }))

    summary = autolearn.repair_skill_use_counts()
    assert summary["updated"] == 1
    assert summary["added"] == 0

    usage = json.loads(ns.usage_file.read_text())
    assert usage["foo"]["created_by"] == "user"  # unchanged
    assert usage["foo"]["use_count"] == 1


# ---------------------------------------------------------------------------
# _scan_skill_dirs_for_repair() — direct unit tests
# ---------------------------------------------------------------------------

# @spec SM-LC-013, SM-LC-015
def test_scan_finds_skills_across_discovery_dirs(isolated_env):
    ns = isolated_env
    _make_skill_on_disk(ns.agents_skills, "alpha")
    _make_skill_on_disk(ns.config_skills, "beta")

    found = autolearn._scan_skill_dirs_for_repair()
    assert set(found.keys()) == {"alpha", "beta"}


def test_scan_skips_archive_dirs(isolated_env):
    ns = isolated_env
    _make_skill_on_disk(ns.agents_skills, "live")
    _make_skill_on_disk(ns.agents_skills / ".archive", "archived-auto")
    _make_skill_on_disk(ns.agents_skills / ".archive-manual", "archived-manual")

    found = autolearn._scan_skill_dirs_for_repair()
    assert set(found.keys()) == {"live"}


def test_scan_skips_dirs_without_skill_md(isolated_env):
    ns = isolated_env
    (ns.agents_skills / "not-a-skill").mkdir()
    (ns.agents_skills / "not-a-skill" / "README.md").write_text("nope")

    _make_skill_on_disk(ns.agents_skills, "real-skill")

    found = autolearn._scan_skill_dirs_for_repair()
    assert set(found.keys()) == {"real-skill"}


def test_scan_first_seen_wins_across_dirs(isolated_env):
    """If a skill name appears in multiple discovery dirs, the first one wins."""
    ns = isolated_env
    # agents_skills is listed before config_skills in the env var.
    _make_skill_on_disk(ns.agents_skills, "dup", contents="# agents version\n")
    _make_skill_on_disk(ns.config_skills, "dup", contents="# config version\n")

    found = autolearn._scan_skill_dirs_for_repair()
    assert len(found) == 1
    assert "dup" in found


# Real ~/.agents/skills/ is a mix of real directories and symlinks to
# ~/.autolearn/personas/default/skills/. Path.is_dir() follows symlinks
# (correct for the scan), and Phase 2's `if name in usage: continue` correctly
# skips autolearn-created skills symlinked into the discovery dir. This test
# pins that behavior so a future refactor can't silently break it.
def test_scan_handles_symlinked_skill_dirs(isolated_env, tmp_path):
    ns = isolated_env
    # A real skill lives outside the discovery dir; a symlink points to it.
    real_skill_dir = tmp_path / "outside_skill_dir"
    real_skill_dir.mkdir()
    (real_skill_dir / "SKILL.md").write_text("# symlinked\n")

    symlink_path = ns.agents_skills / "symlinked-skill"
    symlink_path.symlink_to(real_skill_dir, target_is_directory=True)

    found = autolearn._scan_skill_dirs_for_repair()
    assert "symlinked-skill" in found


# ---------------------------------------------------------------------------
# SM-LC-017 — tracked-manual entries survive curator run (observe-only)
# ---------------------------------------------------------------------------

# @spec SM-LC-017
def test_tracked_manual_entry_not_transitioned_by_curator_run(isolated_env, monkeypatch):
    """A tracked-manual entry passes through curator run unchanged.

    SM-LC-017 guarantees that the retention/lifecycle transition loop skips
    any entry whose created_by is not 'autolearn'. This test crafts a
    tracked-manual entry old enough to trip BOTH the stale (30d) and archive
    (90d) thresholds, runs cmd_curator_run, and asserts the entry is neither
    state-transitioned nor moved to .archive/.
    """
    ns = isolated_env
    # Build a minimal config so load_config() returns the defaults we expect.
    (ns.persona / "config.yaml").write_text(
        "review_threshold: 10\nsession_review_on_idle: true\nmax_conversation_buffer: 50\n"
        "curator_interval_days: 7\nstale_after_days: 30\narchive_after_days: 90\n"
        "escalation_threshold: 3\n"
    )

    # Ancient last_activity so both thresholds are tripped.
    ancient = "2020-01-01"
    ns.usage_file.write_text(json.dumps({
        "old-manual": {
            "created_by": "tracked-manual",
            "created_at": ancient,
            "use_count": 5,
            "patch_count": 0,
            "last_activity_at": ancient,
            "state": "active",
            "pinned": False,
        }
    }))

    # Build a fake argparse Namespace so cmd_curator_run gets what it expects.
    args = type("Args", (), {"persona": "default"})()

    # Run the curator. We don't care about the summary output, only that the
    # entry is preserved unchanged.
    autolearn.cmd_curator_run(args)

    usage = json.loads(ns.usage_file.read_text())
    assert "old-manual" in usage, "tracked-manual skill was dropped"
    entry = usage["old-manual"]
    assert entry["state"] == "active", f"state was transitioned: {entry['state']}"
    assert entry["created_by"] == "tracked-manual"
    # And the skill dir was NOT moved to .archive/ (we never created one on
    # disk; if the curator had tried to archive it, the rename would have
    # failed loudly).
    assert not (ns.skills_dir / ".archive" / "old-manual").exists()


# @spec SM-LC-017 — regression test for the comparison operator.
def test_curator_run_skips_legacy_user_entries_too(isolated_env):
    """The pre-existing 'user' value also benefits from the same exemption."""
    ns = isolated_env
    (ns.persona / "config.yaml").write_text(
        "stale_after_days: 30\narchive_after_days: 90\nescalation_threshold: 3\n"
    )
    ns.usage_file.write_text(json.dumps({
        "legacy-skill": {
            "created_by": "user",  # legacy value
            "created_at": "2020-01-01",
            "use_count": 0,
            "patch_count": 0,
            "last_activity_at": "2020-01-01",
            "state": "active",
            "pinned": False,
        }
    }))

    args = type("Args", (), {"persona": "default"})()
    autolearn.cmd_curator_run(args)

    usage = json.loads(ns.usage_file.read_text())
    assert usage["legacy-skill"]["state"] == "active"
    assert usage["legacy-skill"]["created_by"] == "user"
