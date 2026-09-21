# /// script
# dependencies = ["pytest"]
# ///
"""Tests for outcomes.py — the Certified Procedures shared spine.

Builds a synthetic opencode.db with controlled `part` rows so indexing,
ground-truth classification, skill-linkage, and the reuse-ledger derivation can
be asserted without touching the real ~/.local/share/opencode/opencode.db.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import outcomes


# ---------------------------------------------------------------------------
# Fixtures: a synthetic opencode.db matching the real `part` schema.
# ---------------------------------------------------------------------------

def _make_opencode_db(path: Path, parts: list[dict]) -> None:
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
    for p in parts:
        conn.execute(
            "INSERT INTO part(id, message_id, session_id, time_created, time_updated, data) "
            "VALUES (?,?,?,?,?,?)",
            [p["id"], p["message_id"], p["session_id"], p["time_created"],
             p["time_created"], json.dumps(p["data"])],
        )
    conn.commit()
    conn.close()


@pytest.fixture
def env(tmp_path) -> tuple[Path, Path]:
    """Return (opencode_db, persona_dir)."""
    ocdb = tmp_path / "opencode.db"
    persona = tmp_path / "persona"
    persona.mkdir()
    return ocdb, persona


def _tool_part(pid, sess, t, tool, status="completed", input_=None, output=""):
    return {
        "id": pid, "message_id": f"{pid}-msg", "session_id": sess, "time_created": t,
        "data": {"type": "tool", "tool": tool,
                 "state": {"status": status, "input": input_ or {}, "output": output}},
    }


def _step_part(pid, sess, t, *, input_t=0, output_t=0, reasoning=0, cost=0.0, reason="tool-calls"):
    return {
        "id": pid, "message_id": f"{pid}-msg", "session_id": sess, "time_created": t,
        "data": {"type": "step-finish", "reason": reason, "cost": cost,
                 "tokens": {"input": input_t, "output": output_t, "reasoning": reasoning,
                            "cache": {"read": 0, "write": 0}}},
    }


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------

def test_index_increments_with_high_water_mark(env):
    ocdb, persona = env
    _make_opencode_db(ocdb, [
        _tool_part("p1", "s1", 1000, "bash", output="hi"),
        _tool_part("p2", "s1", 2000, "bash", output="bye"),
    ])
    idx = outcomes.OutcomeIndex(persona / outcomes.OUTCOMES_DB_NAME, ocdb)
    s1 = idx.index()
    assert s1["tool_parts"] == 2
    s2 = idx.index()  # nothing new
    assert s2["tool_parts"] == 0
    assert len(idx.list_tool_outcomes()) == 2
    idx.close()


def test_index_skips_non_tool_non_step_parts(env):
    ocdb, persona = env
    _make_opencode_db(ocdb, [
        {"id": "t1", "message_id": "m", "session_id": "s", "time_created": 1,
         "data": {"type": "text", "text": "hello"}},
        _tool_part("t2", "s", 2, "bash", output="x"),
        {"id": "t3", "message_id": "m", "session_id": "s", "time_created": 3,
         "data": {"type": "reasoning", "text": "..."}},
    ])
    idx = outcomes.OutcomeIndex(persona / outcomes.OUTCOMES_DB_NAME, ocdb)
    s = idx.index()
    assert s["tool_parts"] == 1
    assert s["skipped"] == 2
    idx.close()


def test_full_rebuild_clears(env):
    ocdb, persona = env
    _make_opencode_db(ocdb, [_tool_part("p1", "s1", 1000, "bash", output="x")])
    idx = outcomes.OutcomeIndex(persona / outcomes.OUTCOMES_DB_NAME, ocdb)
    idx.index()
    assert len(idx.list_tool_outcomes()) == 1
    s = idx.index(full=True)
    assert s["tool_parts"] == 1  # re-indexed after truncate
    idx.close()


# ---------------------------------------------------------------------------
# Ground-truth classification (CP-OUT-003)
# ---------------------------------------------------------------------------

def test_classify_gt_test_command_is_strongest(env):
    ocdb, persona = env
    _make_opencode_db(ocdb, [
        _tool_part("t", "s", 1, "bash", output="pytest 2 passed",
                   input_={"command": "uv run pytest"}),
    ])
    idx = outcomes.OutcomeIndex(persona / outcomes.OUTCOMES_DB_NAME, ocdb)
    idx.index()
    row = idx.list_tool_outcomes()[0]
    assert row["gt_strength"] == 4


def test_classify_gt_error_status_is_exit_code(env):
    ocdb, persona = env
    _make_opencode_db(ocdb, [
        _tool_part("t", "s", 1, "bash", status="error", output="boom"),
        _tool_part("t2", "s", 2, "grep", output="matches"),
    ])
    idx = outcomes.OutcomeIndex(persona / outcomes.OUTCOMES_DB_NAME, ocdb)
    idx.index()
    rows = {r["part_id"]: r["gt_strength"] for r in idx.list_tool_outcomes()}
    assert rows["t"] == 3   # error status -> exit-code strength
    assert rows["t2"] == 1  # raw grep output


def test_classify_gt_exit_code_parsed_from_output(env):
    ocdb, persona = env
    _make_opencode_db(ocdb, [
        _tool_part("t", "s", 1, "bash", status="completed",
                   input_={"command": "make build"}, output="Exit code 2"),
    ])
    idx = outcomes.OutcomeIndex(persona / outcomes.OUTCOMES_DB_NAME, ocdb)
    idx.index()
    assert idx.list_tool_outcomes()[0]["gt_strength"] == 3
    assert outcomes.exit_code_of("bash", "completed", {"command": "make build"}, "exit code 2") == 2


def test_classify_gt_metadata_exit_is_ground_truth(env):
    """Real opencode bash parts carry exit in state.metadata.exit (not output text)."""
    ocdb, persona = env
    _make_opencode_db(ocdb, [
        {"id": "p1", "message_id": "m", "session_id": "s", "time_created": 1,
         "data": {"type": "tool", "tool": "bash",
                  "state": {"status": "completed", "input": {"command": "make build"},
                            "output": "", "metadata": {"exit": 2}}}},
    ])
    idx = outcomes.OutcomeIndex(persona / outcomes.OUTCOMES_DB_NAME, ocdb)
    idx.index()
    assert idx.list_tool_outcomes()[0]["gt_strength"] == 3
    assert outcomes.exit_code_of("bash", "completed", {"command": "make build"}, "",
                                 meta_exit=2) == 2


def test_classify_gt_skill_load_is_zero(env):
    ocdb, persona = env
    _make_opencode_db(ocdb, [
        _tool_part("t", "s", 1, "skill", input_={"name": "marimo-pair"},
                   output="## Skill ..."),
    ])
    idx = outcomes.OutcomeIndex(persona / outcomes.OUTCOMES_DB_NAME, ocdb)
    idx.index()
    row = idx.list_tool_outcomes()[0]
    assert row["gt_strength"] == 0
    assert row["skill_name"] == "marimo-pair"


# ---------------------------------------------------------------------------
# Skill linkage + reuse-ledger derivation (CP-OUT-005)
# ---------------------------------------------------------------------------

def test_skill_use_counts_derived_from_skill_loads(env):
    ocdb, persona = env
    _make_opencode_db(ocdb, [
        _tool_part("a", "s1", 1, "skill", input_={"name": "foo"}),
        _tool_part("b", "s1", 2, "skill", input_={"name": "foo"}),
        _tool_part("c", "s2", 3, "skill", input_={"name": "bar"}),
        _tool_part("d", "s2", 4, "bash", output="x"),
    ])
    idx = outcomes.OutcomeIndex(persona / outcomes.OUTCOMES_DB_NAME, ocdb)
    idx.index()
    counts = idx.skill_use_counts()
    assert counts == {"foo": 2, "bar": 1}


# @spec SM-LC-013, SM-LC-014
def test_skill_use_signals_returns_count_and_last_seen(env):
    ocdb, persona = env
    _make_opencode_db(ocdb, [
        _tool_part("a", "s1", 1_000, "skill", input_={"name": "foo"}),
        _tool_part("b", "s1", 5_000, "skill", input_={"name": "foo"}),
        _tool_part("c", "s2", 9_000, "skill", input_={"name": "bar"}),
    ])
    idx = outcomes.OutcomeIndex(persona / outcomes.OUTCOMES_DB_NAME, ocdb)
    idx.index()
    signals = idx.skill_use_signals()
    assert signals["foo"] == {"count": 2, "last_seen_ms": 5_000}
    assert signals["bar"] == {"count": 1, "last_seen_ms": 9_000}


def test_skill_use_signals_empty_when_no_skill_loads(env):
    ocdb, persona = env
    _make_opencode_db(ocdb, [
        _tool_part("a", "s", 1, "bash", output="x"),
    ])
    idx = outcomes.OutcomeIndex(persona / outcomes.OUTCOMES_DB_NAME, ocdb)
    idx.index()
    assert idx.skill_use_signals() == {}


def test_skill_use_signals_excludes_null_skill_name(env):
    """A skill tool call with malformed input (no name) must not pollute signals."""
    ocdb, persona = env
    _make_opencode_db(ocdb, [
        _tool_part("a", "s", 1, "skill", input_={}, output=""),  # no name
        _tool_part("b", "s", 2, "skill", input_={"name": "foo"}),
    ])
    idx = outcomes.OutcomeIndex(persona / outcomes.OUTCOMES_DB_NAME, ocdb)
    idx.index()
    assert idx.skill_use_signals() == {"foo": {"count": 1, "last_seen_ms": 2}}


def test_query_filters_by_min_gt_and_tool(env):
    ocdb, persona = env
    _make_opencode_db(ocdb, [
        _tool_part("a", "s", 1, "grep", output="m"),
        _tool_part("b", "s", 2, "bash", status="error", output="x"),
        _tool_part("c", "s", 3, "bash", output="pytest ok",
                   input_={"command": "pytest"}),
    ])
    idx = outcomes.OutcomeIndex(persona / outcomes.OUTCOMES_DB_NAME, ocdb)
    idx.index()
    assert len(idx.list_tool_outcomes(min_gt=2)) == 2  # error + test
    assert len(idx.list_tool_outcomes(tool="grep")) == 1


# ---------------------------------------------------------------------------
# Step-cost indexing + session ordering
# ---------------------------------------------------------------------------

def test_step_cost_indexed_and_session_sequence_ordered(env):
    ocdb, persona = env
    _make_opencode_db(ocdb, [
        _tool_part("p1", "s", 100, "bash", output="x"),
        _step_part("p2", "s", 200, input_t=10, output_t=5, cost=0.01),
        _tool_part("p3", "s", 300, "bash", output="y"),
    ])
    idx = outcomes.OutcomeIndex(persona / outcomes.OUTCOMES_DB_NAME, ocdb)
    s = idx.index()
    assert s["step_parts"] == 1
    seq = idx.session_sequences("s")
    assert [r["part_id"] for r in seq] == ["p1", "p3"]  # tool rows only, ordered
    idx.close()


def test_status_reports_histogram_and_counts(env):
    ocdb, persona = env
    _make_opencode_db(ocdb, [
        _tool_part("a", "s", 1, "grep", output="m"),
        _tool_part("b", "s", 2, "bash", status="error", output="x"),
        _tool_part("c", "s", 3, "skill", input_={"name": "z"}),
    ])
    idx = outcomes.OutcomeIndex(persona / outcomes.OUTCOMES_DB_NAME, ocdb)
    idx.index()
    st = idx.status()
    assert st["tool_parts"] == 3
    assert st["skill_loads"] == 1
    assert st["gt_histogram"].get("1") == 1  # grep
    assert st["gt_histogram"].get("3") == 1  # error


def test_missing_opencode_db_returns_no_db_marker(env):
    ocdb, persona = env
    # never create ocdb
    idx = outcomes.OutcomeIndex(persona / outcomes.OUTCOMES_DB_NAME, ocdb)
    s = idx.index()
    assert s.get("no_opencode_db") is True
    assert s["tool_parts"] == 0
    idx.close()
