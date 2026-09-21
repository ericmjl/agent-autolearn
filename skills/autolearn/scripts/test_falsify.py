# /// script
# dependencies = ["pytest"]
# ///
"""Tests for falsify.py — Loop 1 (deterministic procedure falsification)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import falsify


# ---------------------------------------------------------------------------
# Claim discovery (CP-FAL-001)
# ---------------------------------------------------------------------------

def test_claims_test_suite_detected(tmp_path):
    skill = tmp_path / "my-skill"
    (skill / "scripts").mkdir(parents=True)
    (skill / "scripts" / "test_foo.py").write_text("def test_x(): assert 1")
    (skill / "SKILL.md").write_text("---\nname: my-skill\n---\n# My Skill\n")
    claims = falsify.claims_of(skill)
    assert claims and claims[0]["method"] == "test-suite"


def test_claims_declared_from_frontmatter(tmp_path):
    skill = tmp_path / "declared-skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\n"
        "name: declared-skill\n"
        "verify:\n"
        "  command: \"pytest -q\"\n"
        "  expect_exit: 0\n"
        "---\n# Declared\n"
    )
    claims = falsify.claims_of(skill)
    assert any(c["method"] == "declared" for c in claims)
    d = next(c for c in claims if c["method"] == "declared")
    assert d["command"] == "pytest -q"
    assert d["expect_exit"] == 0


def test_claims_none_when_nothing_falsifiable(tmp_path):
    skill = tmp_path / "prose-skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: prose-skill\n---\n# Prose only\n")
    assert falsify.claims_of(skill) == []


def test_declared_ranked_above_test_suite(tmp_path):
    """A declared verify: block wins over the bare test-suite heuristic: the
    author's exact command (deps + ignores) is more trustworthy than `pytest scripts/`."""
    skill = tmp_path / "both"
    (skill / "scripts").mkdir(parents=True)
    (skill / "scripts" / "test_a.py").write_text("def test_a(): assert 1")
    (skill / "SKILL.md").write_text(
        "---\nname: both\nverify:\n  command: \"uv run --with pytest pytest scripts/test_a.py -q\"\n---\n# Both\n"
    )
    claims = falsify.claims_of(skill)
    assert claims[0]["method"] == "declared"


# ---------------------------------------------------------------------------
# Declared-claim execution + safety (CP-FAL-002, CP-FAL-003)
# ---------------------------------------------------------------------------

def test_run_declared_pass(tmp_path):
    (tmp_path / "test_ok.py").write_text("def test_ok(): assert True\n")
    res = falsify.run_claim(
        {"method": "declared", "command": "uv run --with pytest pytest test_ok.py -q",
         "expect_exit": 0},
        cwd=tmp_path, timeout=120,
    )
    assert res["verdict"] == "pass"


def test_run_declared_fail_on_wrong_exit(tmp_path):
    (tmp_path / "test_bad.py").write_text("def test_bad(): assert False\n")
    res = falsify.run_claim(
        {"method": "declared", "command": "uv run --with pytest pytest test_bad.py -q",
         "expect_exit": 0},
        cwd=tmp_path, timeout=120,
    )
    assert res["verdict"] == "fail"


def test_run_declared_unsafe_command_is_inconclusive(tmp_path):
    res = falsify.run_claim(
        {"method": "declared", "command": "rm -rf /", "expect_exit": 0},
        cwd=tmp_path, timeout=10,
    )
    assert res["verdict"] == "inconclusive"
    assert "safe subset" in res["evidence"]


def test_run_declared_arbitrary_code_exec_is_inconclusive(tmp_path):
    # contains a test-runner token but hides destructive code in `python -c`
    res = falsify.run_claim(
        {"method": "declared",
         "command": "uv run --with pytest python -c \"import shutil; shutil.rmtree('/tmp/x')\"",
         "expect_exit": 0},
        cwd=tmp_path, timeout=10,
    )
    assert res["verdict"] == "inconclusive"


def test_run_declared_network_command_is_inconclusive(tmp_path):
    res = falsify.run_claim(
        {"method": "declared", "command": "curl https://example.com", "expect_exit": 0},
        cwd=tmp_path, timeout=10,
    )
    assert res["verdict"] == "inconclusive"


# ---------------------------------------------------------------------------
# verify_skill + verdicts (CP-FAL-002)
# ---------------------------------------------------------------------------

def test_verify_skill_no_claim_is_inconclusive(tmp_path):
    skill = tmp_path / "prose"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: prose\n---\n# Prose\n")
    v = falsify.verify_skill(skill)
    assert v["verdict"] == "inconclusive"
    assert v["method"] == "none"


def test_verify_skill_declared_fail_records(tmp_path):
    skill = tmp_path / "bad"
    skill.mkdir()
    (skill / "test_bad.py").write_text("def test_bad(): assert False\n")
    (skill / "SKILL.md").write_text(
        "---\nname: bad\nverify:\n  command: \"uv run --with pytest pytest test_bad.py -q\"\n---\n# Bad\n"
    )
    v = falsify.verify_skill(skill)
    assert v["verdict"] == "fail"
    assert v["skill"] == "bad"


# ---------------------------------------------------------------------------
# verify_all ledger + consequences (CP-FAL-002, CP-FAL-004)
# ---------------------------------------------------------------------------

def _make_skills_dir(tmp_path) -> tuple[Path, Path]:
    skills = tmp_path / "skills"
    skills.mkdir()
    # good: passes declared (real test file)
    good = skills / "good"
    good.mkdir()
    (good / "test_ok.py").write_text("def test_ok(): assert True\n")
    (good / "SKILL.md").write_text(
        "---\nname: good\nverify:\n  command: \"uv run --with pytest pytest test_ok.py -q\"\n---\n# good\n"
    )
    # bad: fails declared (real test file)
    bad = skills / "bad"
    bad.mkdir()
    (bad / "test_bad.py").write_text("def test_bad(): assert False\n")
    (bad / "SKILL.md").write_text(
        "---\nname: bad\nverify:\n"
        "  command: \"uv run --with pytest pytest test_bad.py -q\"\n"
        "  expect_exit: 0\n---\n# bad\n"
    )
    # prose: no claim
    prose = skills / "prose"
    prose.mkdir()
    (prose / "SKILL.md").write_text("---\nname: prose\n---\n# prose\n")
    # archived: skipped
    arch = skills / "archived-one"
    arch.mkdir()
    (arch / "SKILL.md").write_text("---\nname: archived-one\n---\n# arch\n")
    usage = {
        "good": {"state": "active"},
        "bad": {"state": "active"},
        "prose": {"state": "active"},
        "archived-one": {"state": "archived"},
    }
    (skills / ".usage.json").write_text(json.dumps(usage))
    return skills, tmp_path / "verdicts.json"


def test_verify_all_writes_ledger_and_summary(tmp_path):
    skills, verdicts = _make_skills_dir(tmp_path)
    summary = falsify.verify_all(skills, verdicts, dry_run=False)
    assert summary["pass"] == 1   # good
    assert summary["fail"] == 1   # bad
    assert summary["inconclusive"] == 1  # prose (archived skipped)
    ledger = json.loads(verdicts.read_text())
    assert ledger["bad"]["verdict"] == "fail"
    assert ledger["bad"]["fail_count"] == 1
    assert ledger["good"]["fail_count"] == 0
    assert "archived-one" not in ledger


def test_consequences_demotes_failures(tmp_path):
    skills, verdicts = _make_skills_dir(tmp_path)
    falsify.verify_all(skills, verdicts, dry_run=False)
    usage = json.loads((skills / ".usage.json").read_text())
    cons = falsify.apply_consequences(usage, json.loads(verdicts.read_text()), dry_run=False)
    demoted = {d["skill"] for d in cons["demoted"]}
    assert "bad" in demoted
    assert usage["bad"]["state"] == "stale"
    assert usage["good"]["state"] == "active"  # untouched


def test_consequences_pinned_is_flagged_not_demoted(tmp_path):
    skills, verdicts = _make_skills_dir(tmp_path)
    falsify.verify_all(skills, verdicts, dry_run=False)
    usage = json.loads((skills / ".usage.json").read_text())
    usage["bad"]["pinned"] = True
    cons = falsify.apply_consequences(usage, json.loads(verdicts.read_text()), dry_run=False)
    flagged = {d["skill"] for d in cons["flagged"]}
    assert "bad" in flagged
    assert usage["bad"]["state"] == "active"  # NOT demoted (pinned)


def test_dry_run_does_not_write(tmp_path):
    skills, verdicts = _make_skills_dir(tmp_path)
    falsify.verify_all(skills, verdicts, dry_run=True)
    assert not verdicts.exists()


def test_fail_count_accumulates_across_runs(tmp_path):
    skills, verdicts = _make_skills_dir(tmp_path)
    falsify.verify_all(skills, verdicts, dry_run=False)
    falsify.verify_all(skills, verdicts, dry_run=False)
    ledger = json.loads(verdicts.read_text())
    assert ledger["bad"]["fail_count"] == 2
    assert ledger["good"]["fail_count"] == 0
