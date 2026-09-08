# Tests for the wiki layer (WIKI-PAT-*, WIKI-IMP-*).
# Spec: docs/designs/wiki-layer/wiki-layer-EARS.md
from __future__ import annotations

import json
from pathlib import Path

import pytest

import wiki


@pytest.fixture()
def persona(tmp_path: Path) -> Path:
    d = tmp_path / "personas" / "default"
    d.mkdir(parents=True)
    return d


# --- scaffold ---------------------------------------------------------------

# @spec WIKI-PAT-001
def test_scaffold_creates_all_files(tmp_path):
    d = wiki.ensure_scaffold(tmp_path)
    assert (d / "index.md").exists()
    assert (d / "logs.md").exists()
    assert (d / "skill-impact.md").exists()
    assert (d / "patterns").is_dir()
    assert (d / "consolidated").is_dir()


# @spec WIKI-PAT-001
def test_scaffold_idempotent_never_overwrites(tmp_path):
    wiki.ensure_scaffold(tmp_path)
    idx = tmp_path / "wiki" / "index.md"
    idx.write_text("- [keep-me](patterns/keep-me.md): X\n")
    wiki.ensure_scaffold(tmp_path)
    assert "keep-me" in idx.read_text()


# --- pattern pages ----------------------------------------------------------

# @spec WIKI-PAT-002
def test_write_pattern_and_body(tmp_path):
    p = wiki.write_pattern(tmp_path, "commit-hook-blocks-amend",
                           "Commit hook blocks --amend",
                           "Trigger: amending with compose hooks.\n\n"
                           "## Root Cause\n\nprepare-commit-msg runs anyway.\n\n"
                           "## Resolution\n\npipe message via stdin.")
    text = p.read_text()
    assert "Commit hook blocks" in text
    assert "## Evidence" not in text  # no evidence supplied


# @spec WIKI-PAT-002
def test_write_pattern_refuses_duplicate(tmp_path):
    wiki.write_pattern(tmp_path, "dup", "T", "b")
    with pytest.raises(FileExistsError):
        wiki.write_pattern(tmp_path, "dup", "T2", "b2")


# @spec WIKI-PAT-002
def test_write_pattern_line_cap(tmp_path):
    (tmp_path / "config.yaml").write_text("wiki_page_max_lines: 5\n")
    with pytest.raises(ValueError, match="cap is 5"):
        wiki.write_pattern(tmp_path, "long", "T", "\n".join(f"l{i}" for i in range(10)))


# @spec WIKI-PAT-002
def test_write_pattern_rejects_bad_slug(tmp_path):
    with pytest.raises(ValueError):
        wiki.write_pattern(tmp_path, "a/b", "T", "b")


# @spec WIKI-PAT-003
def test_update_pattern_appends_evidence(tmp_path):
    wiki.write_pattern(tmp_path, "p1", "T", "body",
                       session_ids=["ses_aaa"])
    wiki.update_pattern(tmp_path, "p1", add_evidence=["ses_bbb", "ses_aaa"],
                        add_resolution="second workaround found")
    text = (tmp_path / "wiki" / "patterns" / "p1.md").read_text()
    assert "ses_bbb" in text
    assert "ses_aaa" in text
    assert "second workaround found" in text
    # no duplicate evidence line
    assert text.count("ses_aaa") == 1


# @spec WIKI-PAT-003
def test_update_pattern_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        wiki.update_pattern(tmp_path, "nope", add_evidence=["ses_x"])


# --- index ------------------------------------------------------------------

# @spec WIKI-PAT-004
def test_upsert_index_one_line_per_pattern(tmp_path):
    wiki.upsert_index_entry(tmp_path, "s1", "problem + cause + fix")
    wiki.upsert_index_entry(tmp_path, "s2", "other problem")
    wiki.upsert_index_entry(tmp_path, "s1", "problem + better fix")
    idx = (tmp_path / "wiki" / "index.md").read_text()
    assert idx.count("[s1]") == 1
    assert "better fix" in idx


# --- logs & ledger ----------------------------------------------------------

# @spec WIKI-IMP-001, WIKI-IMP-002
def test_append_skill_impact_appends_only(tmp_path):
    wiki.append_skill_impact(tmp_path, action="create", skill="s1",
                             summary="first", outcome="accepted", trigger="review")
    wiki.append_skill_impact(tmp_path, action="patch", skill="s1",
                             summary="rejected attempt X", outcome="rejected",
                             trigger="review")
    f = tmp_path / "wiki" / "skill-impact.md"
    lines = [json.loads(l) for l in f.read_text().splitlines() if l.startswith("{")]
    assert [e["outcome"] for e in lines] == ["accepted", "rejected"]
    assert lines[1]["summary"] == "rejected attempt X"  # kept forever


# @spec WIKI-IMP-003
def test_rejected_approaches(tmp_path):
    wiki.append_skill_impact(tmp_path, action="patch", skill="s",
                             summary="bad idea", outcome="rejected", trigger="review")
    wiki.append_skill_impact(tmp_path, action="demote", skill="s",
                             summary="stale claim", outcome="demoted", trigger="falsify")
    wiki.append_skill_impact(tmp_path, action="create", skill="s2",
                             summary="good", outcome="accepted", trigger="proposer")
    assert wiki.rejected_approaches(tmp_path) == ["bad idea", "stale claim"]


# @spec WIKI-PAT-006
def test_append_log_counts(tmp_path):
    wiki.append_log(tmp_path, summary="review ran", counts={"patterns": 1})
    log = (tmp_path / "wiki" / "logs.md").read_text()
    assert "review ran" in log and "patterns=1" in log


# --- retrieval --------------------------------------------------------------

# @spec WIKI-PAT-005
def test_find_relevant_patterns_ranks(tmp_path):
    wiki.upsert_index_entry(tmp_path, "git-hooks", "commit hook blocks amend; pipe message via stdin")
    wiki.upsert_index_entry(tmp_path, "uv-tools", "use uv run for python scripts")
    hits = wiki.find_relevant_patterns(tmp_path, "commit hook amend")
    assert hits[0]["slug"] == "git-hooks"


# --- composed view ----------------------------------------------------------

# @spec WIKI-PAT-006
def test_compose_context_sections(tmp_path):
    wiki.ensure_scaffold(tmp_path)
    wiki.append_skill_impact(tmp_path, action="create", skill="s",
                             summary="origin", outcome="accepted", trigger="review")
    wiki.append_log(tmp_path, summary="day one")
    md = wiki.compose_context(tmp_path)
    assert "## Pattern Index" in md
    assert "## Recent Evolution Log" in md
    assert "## Recent Skill Impact" in md
    assert "DO NOT repeat" in md
    assert "origin" in md


# --- PURPOSE.md -------------------------------------------------------------

# @spec WIKI-SKL-001, WIKI-SKL-002
def test_write_purpose_create_then_patch(tmp_path):
    sd = tmp_path / "skills" / "s1"
    p = wiki.write_purpose(sd, origin="from pattern x", patterns=["x"],
                           evolution="")
    assert (p).exists() and "from pattern x" in p.read_text()
    wiki.write_purpose(sd, origin="ignored", patterns=[], evolution="added rule Y")
    text = p.read_text()
    assert text.count("## Evolution History") == 1
    assert "added rule Y" in text
    assert "Patterns Addressed" in text


def test_enabled_false(tmp_path):
    (tmp_path / "config.yaml").write_text("wiki_enabled: false\n")
    assert wiki.enabled(tmp_path) is False
    assert wiki.enabled(tmp_path / "missing") is True  # fail-open default