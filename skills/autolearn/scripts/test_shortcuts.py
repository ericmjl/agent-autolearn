# /// script
# dependencies = ["pytest"]
# ///
"""Tests for shortcuts.py — Loop 2 (roundabout detection + golden-path gating)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import outcomes
import shortcuts


# ---------------------------------------------------------------------------
# Build a synthetic outcomes.db with controlled ordered tool_outcome + step_cost
# ---------------------------------------------------------------------------

def _idx(tmp_path) -> outcomes.OutcomeIndex:
    persona = tmp_path / "persona"
    idx = outcomes.OutcomeIndex(persona / outcomes.OUTCOMES_DB_NAME, tmp_path / "opencode.db")
    idx.init_schema()
    return idx


def _tool(idx, sess, seq, tool, status, command=None, skill=None):
    inp = {}
    if command is not None:
        inp["command"] = command
    if skill is not None:
        inp["name"] = skill
    idx.conn.execute(
        "INSERT INTO tool_outcome(part_id, session_id, message_id, seq, time_created, "
        "tool, status, skill_name, input_json, output_peek, gt_strength) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [f"p{seq}", sess, f"p{seq}-m", seq, seq, tool, status, skill,
         json.dumps(inp), "", 1],
    )


def _step(idx, sess, seq, tokens):
    idx.conn.execute(
        "INSERT INTO step_cost(part_id, session_id, seq, time_created, reason, "
        "tokens_in, tokens_out, tokens_reasoning, cache_read, cache_write, cost) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [f"s{seq}", sess, seq, seq, "tool-calls", tokens, 0, 0, 0, 0, 0.0],
    )


# ---------------------------------------------------------------------------
# Roundabout detection (CP-SHO-001)
# ---------------------------------------------------------------------------

def test_help_chain_detected_with_golden_command(tmp_path):
    idx = _idx(tmp_path)
    # 3 help probes then the working command
    for i, cmd in enumerate(["foo --help", "foo bar --help", "foo bar baz --help"], start=10):
        _tool(idx, "sess", i, "bash", "completed", command=cmd)
    _tool(idx, "sess", 14, "bash", "completed", command="foo bar baz --flag value")
    idx.conn.commit()
    cands = shortcuts.detect_roundabouts(idx, config={"roundabout_help_depth": 2,
                                                       "roundabout_recent_sessions": 5})
    chains = [c for c in cands if c["kind"] == "help-chain"]
    assert len(chains) == 1
    assert chains[0]["golden_command"] == "foo bar baz --flag value"
    assert chains[0]["run_length"] == 3
    idx.close()


def test_help_chain_below_depth_not_detected(tmp_path):
    idx = _idx(tmp_path)
    _tool(idx, "sess", 1, "bash", "completed", command="foo --help")
    _tool(idx, "sess", 2, "bash", "completed", command="foo bar")
    idx.conn.commit()
    cands = shortcuts.detect_roundabouts(idx, config={"roundabout_help_depth": 2,
                                                       "roundabout_recent_sessions": 5})
    assert not [c for c in cands if c["kind"] == "help-chain"]
    idx.close()


def test_error_run_detected_with_golden_command(tmp_path):
    idx = _idx(tmp_path)
    # 3 errors then a completed call with the same tool
    for i in (1, 2, 3):
        _tool(idx, "sess", i, "bash", "error", command=f"make build{i}")
    _tool(idx, "sess", 4, "bash", "completed", command="make build")
    idx.conn.commit()
    cands = shortcuts.detect_roundabouts(idx, config={"roundabout_error_run": 3,
                                                       "roundabout_help_depth": 99,
                                                       "roundabout_recent_sessions": 5})
    runs = [c for c in cands if c["kind"] == "error-run"]
    assert len(runs) == 1
    assert runs[0]["golden_command"] == "make build"
    assert runs[0]["run_length"] == 3
    idx.close()


def test_error_run_with_different_tool_successor_records_null_golden(tmp_path):
    idx = _idx(tmp_path)
    _tool(idx, "sess", 1, "bash", "error")
    _tool(idx, "sess", 2, "bash", "error")
    _tool(idx, "sess", 3, "bash", "error")
    _tool(idx, "sess", 4, "grep", "completed", command="grep x")  # different tool
    idx.conn.commit()
    cands = shortcuts.detect_roundabouts(idx, config={"roundabout_error_run": 3,
                                                       "roundabout_help_depth": 99,
                                                       "roundabout_recent_sessions": 5})
    runs = [c for c in cands if c["kind"] == "error-run"]
    assert len(runs) == 1                     # recorded (CP-SHO-002) ...
    assert runs[0]["golden_command"] is None  # ... but not promotable (different tool)
    idx.close()


def test_help_chain_unrelated_golden_is_null(tmp_path):
    idx = _idx(tmp_path)
    _tool(idx, "sess", 1, "bash", "completed", command="foo --help")
    _tool(idx, "sess", 2, "bash", "completed", command="foo --help")
    _tool(idx, "sess", 3, "bash", "completed", command="bar run")  # unrelated first word
    idx.conn.commit()
    cands = shortcuts.detect_roundabouts(idx, config={"roundabout_help_depth": 2,
                                                       "roundabout_recent_sessions": 5})
    chains = [c for c in cands if c["kind"] == "help-chain"]
    assert len(chains) == 1
    assert chains[0]["golden_command"] is None  # relevance check (M6) rejects it
    idx.close()


def test_help_chain_no_terminal_success_is_null(tmp_path):
    idx = _idx(tmp_path)
    _tool(idx, "sess", 1, "bash", "completed", command="foo --help")
    _tool(idx, "sess", 2, "bash", "completed", command="foo --help")
    idx.conn.commit()  # run off the end — no successor
    cands = shortcuts.detect_roundabouts(idx, config={"roundabout_help_depth": 2,
                                                       "roundabout_recent_sessions": 5})
    chains = [c for c in cands if c["kind"] == "help-chain"]
    assert len(chains) == 1                     # recorded as pure-cost finding (CP-SHO-002)
    assert chains[0]["golden_command"] is None
    idx.close()


# ---------------------------------------------------------------------------
# Token cost + ordering (CP-SHO-001)
# ---------------------------------------------------------------------------

def test_cost_is_summed_and_candidates_sorted_desc(tmp_path):
    idx = _idx(tmp_path)
    _tool(idx, "sess", 10, "bash", "completed", command="x --help")
    _tool(idx, "sess", 11, "bash", "completed", command="x y --help")
    _tool(idx, "sess", 12, "bash", "completed", command="x y z run")
    _step(idx, "sess", 10, 1000)
    _step(idx, "sess", 11, 1500)
    _step(idx, "sess", 12, 500)
    idx.conn.commit()
    cands = shortcuts.detect_roundabouts(idx, config={"roundabout_help_depth": 2,
                                                       "roundabout_recent_sessions": 5})
    assert cands[0]["cost_tokens"] == 3000  # 1000+1500+500
    idx.close()


# ---------------------------------------------------------------------------
# Promotion gating + min-savings (CP-SHO-003, CP-SHO-004)
# ---------------------------------------------------------------------------

def test_promotable_filters_by_min_tokens_and_requires_golden():
    cands = [
        {"kind": "help-chain", "cost_tokens": 5000, "golden_command": "foo run"},
        {"kind": "help-chain", "cost_tokens": 500, "golden_command": "foo run"},
        {"kind": "error-run", "cost_tokens": 9000, "golden_command": None},
    ]
    prom = shortcuts.promotable(cands, config={"shortcut_promote_min_tokens": 2000})
    assert len(prom) == 1
    assert prom[0]["golden_command"] == "foo run"


def test_verify_candidate_safe_pass(tmp_path):
    (tmp_path / "test_ok.py").write_text("def test_ok(): assert True\n")
    c = {"golden_command": "uv run --with pytest pytest test_ok.py -q"}
    res = shortcuts.verify_candidate(c, config={"shortcut_verify_timeout_s": 120}, cwd=tmp_path)
    assert res["verdict"] == "pass"


def test_verify_candidate_unsafe_is_inconclusive():
    c = {"golden_command": "rm -rf /"}
    res = shortcuts.verify_candidate(c, config={"shortcut_verify_timeout_s": 10})
    assert res["verdict"] == "inconclusive"


def test_verify_candidate_no_command_is_inconclusive():
    res = shortcuts.verify_candidate({"golden_command": None})
    assert res["verdict"] == "inconclusive"
