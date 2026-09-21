# /// script
# dependencies = ["pytest"]
# ///
"""Tests for proposer.py — long-horizon skill proposal loop."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import proposer


# ---------------------------------------------------------------------------
# Fixtures: synthetic search.db + outcomes.db in a persona dir
# ---------------------------------------------------------------------------

def _persona(tmp_path) -> Path:
    p = tmp_path / "persona"
    p.mkdir()
    return p


def _search_db(persona: Path, sessions: dict[str, list[tuple[str, str]]]) -> None:
    conn = sqlite3.connect(str(persona / "search.db"))
    conn.executescript(
        "CREATE TABLE session_text_content("
        "rowid INTEGER PRIMARY KEY, session_id TEXT NOT NULL, message_id TEXT NOT NULL, "
        "role TEXT NOT NULL, text TEXT NOT NULL, project TEXT NOT NULL DEFAULT '', "
        "timestamp INTEGER NOT NULL);"
    )
    rid = 0
    for sid, msgs in sessions.items():
        for i, (role, text) in enumerate(msgs):
            conn.execute(
                "INSERT INTO session_text_content(rowid, session_id, message_id, role, text, project, timestamp) "
                "VALUES (?,?,?,?,?,?,?)",
                [rid, sid, f"{sid}-m{i}", role, text, "", i],
            )
            rid += 1
    conn.commit()
    conn.close()


def _idx(persona: Path) -> "proposer.outcomes.OutcomeIndex":
    idx = proposer.outcomes.OutcomeIndex(persona / proposer.outcomes.OUTCOMES_DB_NAME,
                                         persona / "opencode.db")
    return idx


def _tool(idx, sess, seq, tool, status, command=None):
    inp = {"command": command} if command else {}
    idx.conn.execute(
        "INSERT INTO tool_outcome(part_id, session_id, message_id, seq, time_created, "
        "tool, status, skill_name, input_json, output_peek, gt_strength) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [f"p{sess}{seq}", sess, f"p{sess}{seq}-m", seq, seq, tool, status, None,
         json.dumps(inp), "", 1],
    )


# A substantive user message that yields a stable topic signature
REQUEST = "please run the project test suite before committing changes"


def _env_with_cluster(tmp_path, n_sessions=3, resolutions=None):
    persona = _persona(tmp_path)
    sessions = {f"s{i}": [("user", REQUEST), ("assistant", "ok")] for i in range(n_sessions)}
    _search_db(persona, sessions)
    idx = _idx(persona)
    resolutions = resolutions or ["uv run pytest"] * n_sessions
    for i, cmd in enumerate(resolutions):
        _tool(idx, f"s{i}", 10 + i, "bash", "completed", command=cmd)
    idx.conn.commit()
    idx.close()
    return persona


# ---------------------------------------------------------------------------
# scan (LH-PROP-001)
# ---------------------------------------------------------------------------

def test_scan_stages_recurring_cluster(tmp_path):
    persona = _env_with_cluster(tmp_path, n_sessions=3)
    summary = proposer.scan(persona, config={"proposer_min_sessions": 3, "proposer_recent_sessions": 50})
    assert summary["new"] == 1
    proposals = proposer._load(persona)
    assert len(proposals) == 1
    p = next(iter(proposals.values()))
    assert p["sessions_count"] == 3
    assert p["status"] == "pending"
    assert p["common_resolution"] == "uv run pytest"


def test_scan_below_threshold_no_proposal(tmp_path):
    persona = _env_with_cluster(tmp_path, n_sessions=2)
    proposer.scan(persona, config={"proposer_min_sessions": 3, "proposer_recent_sessions": 50})
    assert proposer._load(persona) == {}


def test_scan_modal_resolution(tmp_path):
    # 3 sessions, two with one command, one with another -> modal wins
    persona = _env_with_cluster(tmp_path, n_sessions=3,
                                resolutions=["uv run pytest", "uv run pytest", "make test"])
    proposer.scan(persona, config={"proposer_min_sessions": 3})
    p = next(iter(proposer._load(persona).values()))
    assert p["common_resolution"] == "uv run pytest"


def test_scan_is_idempotent_and_accumulates(tmp_path):
    persona = _env_with_cluster(tmp_path, n_sessions=3)
    proposer.scan(persona, config={"proposer_min_sessions": 3})
    # re-scan: same cluster -> updated, not new
    summary = proposer.scan(persona, config={"proposer_min_sessions": 3})
    assert summary["new"] == 0
    assert summary["updated"] == 1


def test_scan_clusters_varied_wording_via_jaccard(tmp_path):
    """The whole point: differently-worded requests for the same thing cluster."""
    persona = _persona(tmp_path)
    sessions = {
        "s1": [("user", "please run the project test suite before committing")],
        "s2": [("user", "run the test suite for this repo")],
        "s3": [("user", "how do I run all the tests here")],
    }
    _search_db(persona, sessions)
    idx = _idx(persona)
    # NOTE: outcomes session ids MUST match the search.db session ids —
    # recent_sessions() reads from outcomes, _session_request reads from search.
    for sid in ("s1", "s2", "s3"):
        _tool(idx, sid, 10, "bash", "completed", command="uv run pytest")
    idx.conn.commit(); idx.close()
    summary = proposer.scan(persona, config={"proposer_min_sessions": 3})
    assert summary["new"] == 1, "varied wordings of the same request must cluster"


def test_is_recurrent_clusters_varied_wording(tmp_path):
    persona = _persona(tmp_path)
    sessions = {
        "s1": [("user", "run the project test suite before committing")],
        "s2": [("user", "run the test suite for this repo")],
        "s3": [("user", "how do I run all the tests")],
    }
    _search_db(persona, sessions)
    idx = _idx(persona); idx.conn.commit(); idx.close()
    res = proposer.is_recurrent(persona, "please run the tests now", config={"proposer_min_sessions": 3})
    assert res["recurrent"] is True


# ---------------------------------------------------------------------------
# is_recurrent (LH-PROP-004) — the reviewer hard-gate
# ---------------------------------------------------------------------------

def test_is_recurrent_true(tmp_path):
    persona = _env_with_cluster(tmp_path, n_sessions=3)
    res = proposer.is_recurrent(persona, REQUEST, config={"proposer_min_sessions": 3})
    assert res["recurrent"] is True
    assert res["sessions_count"] >= 3


def test_is_recurrent_false_for_novel_request(tmp_path):
    persona = _env_with_cluster(tmp_path, n_sessions=3)
    res = proposer.is_recurrent(persona, "totally different unrelated request about docker networking",
                                config={"proposer_min_sessions": 3})
    assert res["recurrent"] is False


def test_is_recurrent_fail_safe_when_no_search_db(tmp_path):
    persona = _persona(tmp_path)
    idx = _idx(persona); idx.close()
    res = proposer.is_recurrent(persona, REQUEST, config={"proposer_min_sessions": 3})
    assert res["recurrent"] is False  # fail-safe: don't block the reviewer


# ---------------------------------------------------------------------------
# verify_pending + promote_ready (LH-PROP-002, LH-PROP-003)
# ---------------------------------------------------------------------------

def _stage(persona, command):
    """Stage one pending proposal with the given common_resolution."""
    proposer._save(persona, {
        "abc123def45678901": {
            "id": "abc123def45678901", "request_signature": "abc123def45678901",
            "request_summary": "x", "common_resolution": command,
            "session_ids": ["s1", "s2", "s3"], "sessions_count": 3,
            "est_tokens_saved": 0, "first_seen": "2026-07-19", "last_seen": "2026-07-19",
            "status": "pending", "verified": None, "promoted_skill": None, "updated_at": "2026-07-19",
        }
    })


def test_verify_pending_passing_safe_command(tmp_path):
    persona = _persona(tmp_path)
    _stage(persona, "uv run --with pytest pytest --version")  # safe + exits 0
    res = proposer.verify_pending(persona)
    assert res["checked"] == 1
    p = proposer._load(persona)["abc123def45678901"]
    assert p["verified"] is True


def test_verify_pending_unsafe_command_stays_null(tmp_path):
    persona = _persona(tmp_path)
    _stage(persona, "make build")  # no test-runner token -> not run -> inconclusive -> None
    proposer.verify_pending(persona)
    p = proposer._load(persona)["abc123def45678901"]
    assert p["verified"] is None


def test_promote_ready_returns_only_verified(tmp_path):
    persona = _persona(tmp_path)
    proposals = proposer._load(persona)
    proposals["p1"] = {"id": "p1", "status": "pending", "verified": True, "common_resolution": "x"}
    proposals["p2"] = {"id": "p2", "status": "pending", "verified": False, "common_resolution": "y"}
    proposals["p3"] = {"id": "p3", "status": "pending", "verified": None, "common_resolution": "z"}
    proposals["p4"] = {"id": "p4", "status": "promoted", "verified": True, "common_resolution": "w"}
    proposer._save(persona, proposals)
    ready = proposer.promote_ready(persona)
    assert [p["id"] for p in ready] == ["p1"]


# ---------------------------------------------------------------------------
# confirm / dismiss
# ---------------------------------------------------------------------------

def test_dismiss_marks_status(tmp_path):
    persona = _persona(tmp_path)
    _stage(persona, "uv run pytest")
    proposals = proposer._load(persona)
    proposals["abc123def45678901"]["status"] = "dismissed"
    proposer._save(persona, proposals)
    assert proposer._load(persona)["abc123def45678901"]["status"] == "dismissed"
