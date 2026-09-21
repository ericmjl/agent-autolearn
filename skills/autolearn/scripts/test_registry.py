# /// script
# dependencies = ["pytest", "python-slugify"]
# ///
"""Tests for registry.py — run via `uv run pytest test_registry.py -q`."""
import json
from pathlib import Path

import pytest

import registry as R


def persona(tmp_path: Path) -> Path:
    p = tmp_path / "default"
    p.mkdir()
    return p


def test_slugify_and_topics():
    assert R.slugify_text("Use uv tool for Python CLI tools!!") == "use-uv-tool-for-python-cli-tools"
    topics = R.extract_topics("Use uv tool for Python CLI tools, never pip3")
    assert "python" in topics and "uv" in topics and "the" not in topics
    assert len(set(topics)) == len(topics)  # dedup


def test_topic_signature_stable():
    sig1, t1 = R.topic_signature("use uv tool for python")
    sig2, t2 = R.topic_signature("python tool uv use for")  # same tokens, diff order
    assert sig1 == sig2


def test_add_find_reinforce(tmp_path):
    reg = R.MemoryRegistry(persona(tmp_path))
    rec = reg.add("Never use pip3; use uv tool", type="memory")
    assert reg.find_by_text("Never use pip3; use uv tool")["id"] == rec["id"]
    # distinct days each count as a reinforcement
    reg.reinforce(rec["id"], when="2026-06-01")
    reg.reinforce(rec["id"], when="2026-06-10")
    got = reg.get(rec["id"])
    assert len(got["reinforcements"]) == 2
    assert got["last_reinforced"] == "2026-06-10"
    assert got["status"] == "active"


def test_reinforce_same_day_dedup(tmp_path):
    reg = R.MemoryRegistry(persona(tmp_path))
    rec = reg.add("dedup me")
    reg.reinforce(rec["id"], when="2026-06-01")
    reg.reinforce(rec["id"], when="2026-06-01")  # same day → deduped
    assert len(reg.get(rec["id"])["reinforcements"]) == 1


def test_reinforce_revives_evicted(tmp_path):
    reg = R.MemoryRegistry(persona(tmp_path))
    rec = reg.add("prefers concise responses")
    reg.set_status(rec["id"], "evicted", "2026-01-01")
    assert reg.get(rec["id"])["status"] == "evicted"
    reg.reinforce(rec["id"])  # @spec MI-REG-007
    got = reg.get(rec["id"])
    assert got["status"] == "active" and got["evicted_at"] is None


def test_save_atomic_and_load_active(tmp_path):
    reg = R.MemoryRegistry(persona(tmp_path))
    a = reg.add("active one")
    b = reg.add("to evict")
    reg.set_status(b["id"], "evicted", "2026-01-01")
    active = reg.load_active()
    assert len(active) == 1 and active[0]["id"] == a["id"]
    # no leftover tmp file
    assert not (reg.path.with_suffix(reg.path.suffix + ".tmp")).exists()


def test_corrupt_line_skipped(tmp_path):
    reg = R.MemoryRegistry(persona(tmp_path))
    reg.add("good entry")
    with reg.path.open("a", encoding="utf-8") as fh:
        fh.write("{not valid json\n")
    records = reg.load()  # must not raise
    assert len(records) == 1


def test_migration_from_legacy(tmp_path):
    p = persona(tmp_path)
    (p / "memory.md").write_text(
        "# Autolearn Memory\n\n<!-- Managed by autolearn. -->\n\n"
        "- Always use uv for python tools\n"
        "- Never commit secrets\n",
        encoding="utf-8",
    )
    (p / "user-profile.md").write_text(
        "# User Profile\n\n<!-- Managed by autolearn. -->\n\n"
        "- Prefers concise responses\n",
        encoding="utf-8",
    )
    slug = R.slugify_text("Always use uv for python tools")
    (p / "strengths.json").write_text(json.dumps({
        slug: {"count": 4, "first_seen": "2026-06-01", "last_seen": "2026-06-10"},
    }), encoding="utf-8")

    reg = R.MemoryRegistry(p)
    records = reg.load()  # triggers migration
    by_text = {r["text"]: r for r in records}
    assert "Always use uv for python tools" in by_text
    uv = by_text["Always use uv for python tools"]
    assert uv["type"] == "memory"
    assert uv["created_at"] == "2026-06-01"
    assert len(uv["reinforcements"]) == 3  # count-1
    assert by_text["Prefers concise responses"]["type"] == "user"
    # legacy memory.md renamed
    assert not (p / "memory.md").exists()
    assert (p / "memory.md.legacy").exists()
    # idempotent: second load does not re-migrate / duplicate
    n1 = len(reg.load())
    assert n1 == len(reg.load())


def test_update_and_remove(tmp_path):
    reg = R.MemoryRegistry(persona(tmp_path))
    rec = reg.add("a rule")
    rec["retention_score"] = 0.9
    rec["tier"] = "hot"
    reg.update(rec)
    assert reg.get(rec["id"])["tier"] == "hot"
    assert reg.remove(rec["id"]) is True
    assert reg.get(rec["id"]) is None


def test_migration_with_zero_records_materializes_registry(tmp_path):
    # Regression: a fresh install seeds memory.md / user-profile.md with headers
    # only (no entries). Migration must still create an empty memories.jsonl so
    # the installer's verification step passes and subsequent load() calls don't
    # rely on lazy creation being re-triggered (the .registry_migrated marker
    # would short-circuit migrate_from_legacy on every later call).
    p = persona(tmp_path)
    (p / "memory.md").write_text(
        "# Autolearn Memory\n\n<!-- Managed by autolearn. -->\n\n",
        encoding="utf-8",
    )
    (p / "user-profile.md").write_text(
        "# User Profile\n\n<!-- Managed by autolearn. -->\n\n",
        encoding="utf-8",
    )

    reg = R.MemoryRegistry(p)
    records = reg.load()  # triggers migration
    assert records == []
    assert reg.path.exists()  # memories.jsonl materialized even though empty
    assert reg.load() == []   # readable, still empty
