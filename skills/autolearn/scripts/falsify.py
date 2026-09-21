"""Falsify — Loop 1 of Certified Procedures. Verify skills deterministically.

A skill is autolearn's program-analog. This module checks whether a skill's
procedure still produces its claimed outcome, and demotes + flags failures.

Deterministic-first scope (per Decision 12):
  * ``test-suite`` claim — the skill ships ``scripts/test_*.py``. Run pytest;
    exit 0 -> pass, exit 1 -> fail, anything else (collection error / missing
    dep / timeout) -> inconclusive. Best-effort: a skill whose tests need deps
    pytest can't see comes back inconclusive, not fail.
  * ``declared`` claim   — SKILL.md frontmatter ``verify:`` block names the
    exact command (+ expected exit). This is the robust path: the author
    controls the environment. Only a safe command subset is executed.
  * ``none``             — no claim -> inconclusive, untouched.

Probabilistic (correlation-based) falsification is deferred and, when added,
is suggestion-only — it never auto-demotes (CP-FAL-005).

Spec: docs/designs/certified-procedures/LLD.md, certified-procedures-EARS.md (CP-FAL-*).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

import outcomes

DEFAULT_CONFIG = {
    "falsify_fail_demote_after": 1,
    "falsify_run_timeout_s": 120,
}

VERDICTS_FILE = "verdicts.json"

# Safe-subset policy for a declared claim (CP-FAL-003). A declared claim must
# be a test-runner invocation; arbitrary code execution / network / destructive
# verbs are refused and the claim comes back inconclusive rather than executed.
SAFE_DECLARED_TOKENS = outcomes.SAFE_DECLARED_TOKENS
SAFE_DECLARED_DENY = outcomes.SAFE_DECLARED_DENY

_TEST_CMD = "uv run --with pytest pytest scripts/ -q"


# ---------------------------------------------------------------------------
# Claim discovery
# ---------------------------------------------------------------------------

def _read_frontmatter_verify(skill_md: Path) -> dict | None:
    """Minimal, stdlib-only parse of a ``verify:`` block from SKILL.md YAML
    frontmatter. Returns {"command": str, "expect_exit": int} or None.

    Only the narrow schema we define is supported; anything else -> None.
    """
    if not skill_md.exists():
        return None
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", text, re.DOTALL)
    if not m:
        return None
    fm = m.group(1)
    # locate a top-level `verify:` block
    i = fm.find("\nverify:")
    if i < 0 and fm.startswith("verify:"):
        i = -1  # block at very top
    if i < 0 and not fm.startswith("verify:"):
        return None
    start = 0 if fm.startswith("verify:") else i + 1
    lines = fm[start:].splitlines()
    # lines[0] == "verify:"
    if not lines or not lines[0].strip().rstrip(":").startswith("verify"):
        return None
    command = None
    expect_exit = 0
    in_block = False
    for ln in lines[1:]:
        if ln.startswith("  "):
            in_block = True
            s = ln.strip()
            cm = re.match(r'command:\s*["\']?(.+?)["\']?\s*$', s)
            if cm:
                command = cm.group(1).strip()
            em = re.match(r'expect_exit:\s*([0-9]+)\s*$', s)
            if em:
                expect_exit = int(em.group(1))
        elif in_block:
            break  # block ended
    if not command:
        return None
    return {"command": command, "expect_exit": expect_exit}


# @spec CP-FAL-001
def claims_of(skill_dir: Path) -> list[dict]:
    """Falsifiable claims, strongest first: declared > test-suite > none.

    A declared ``verify:`` block wins because the author specified the exact
    command (deps, ignores, etc.); the bare test-suite command is a best-effort
    fallback for skills that ship tests but no declared block, and may come back
    inconclusive when those tests need deps the bare command can't resolve.
    """
    claims: list[dict] = []
    declared = _read_frontmatter_verify(skill_dir / "SKILL.md")
    if declared:
        claims.append({"method": "declared", **declared})
    scripts_dir = skill_dir / "scripts"
    if scripts_dir.is_dir():
        tests = sorted(scripts_dir.glob("test_*.py"))
        if tests:
            claims.append({"method": "test-suite",
                           "tests": [str(p.relative_to(skill_dir)) for p in tests]})
    return claims


def _is_safe_declared(command: str) -> bool:
    """A declared verify command is safe iff it is a test-runner invocation and
    contains no arbitrary-exec / network / destructive tokens (CP-FAL-003)."""
    c = command.strip()
    if SAFE_DECLARED_DENY.search(c):
        return False
    return any(tok in c for tok in SAFE_DECLARED_TOKENS)


# @spec CP-FAL-002, CP-FAL-003
def run_claim(claim: dict, *, cwd: Path, timeout: int) -> dict:
    """Execute one claim. Returns {verdict, evidence, method}."""
    method = claim["method"]
    if method == "test-suite":
        cmd = _TEST_CMD
    elif method == "declared":
        cmd = claim["command"]
        if not _is_safe_declared(cmd):
            return {"verdict": "inconclusive", "method": method,
                    "evidence": "declared command outside safe subset; not executed"}
    else:
        return {"verdict": "inconclusive", "method": method, "evidence": "unknown method"}

    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=str(cwd), timeout=timeout,
            capture_output=True, text=True,
        )
    except subprocess.TimeoutExpired:
        return {"verdict": "inconclusive", "method": method,
                "evidence": f"timed out after {timeout}s"}
    except (OSError, ValueError) as exc:
        return {"verdict": "inconclusive", "method": method,
                "evidence": f"could not run: {exc}"}

    rc = proc.returncode
    tail = (proc.stderr or proc.stdout or "").strip().splitlines()
    tail_str = "\n".join(tail[-6:])

    if method == "test-suite":
        # pytest exit codes: 0 pass, 1 fail, 2+ interrupted/internal/no-tests
        if rc == 0:
            return {"verdict": "pass", "method": method, "evidence": "test-suite green"}
        if rc == 1:
            return {"verdict": "fail", "method": method,
                    "evidence": "test-suite reported failures" + (f":\n{tail_str}" if tail_str else "")}
        return {"verdict": "inconclusive", "method": method,
                "evidence": f"pytest exit {rc} (collection/dep error?)"
                + (f":\n{tail_str}" if tail_str else "")}

    # declared
    expect = int(claim.get("expect_exit", 0))
    if rc == expect:
        return {"verdict": "pass", "method": method,
                "evidence": f"exit {rc} == expected {expect}"}
    return {"verdict": "fail", "method": method,
            "evidence": f"exit {rc} != expected {expect}"
            + (f":\n{tail_str}" if tail_str else "")}


# @spec CP-FAL-002
def verify_skill(skill_dir: Path, *, config: dict | None = None) -> dict:
    """Verify one skill; return a verdict record (see VERDICTS_FILE schema)."""
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    claims = claims_of(skill_dir)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    if not claims:
        return {"skill": skill_dir.name, "verdict": "inconclusive",
                "method": "none", "evidence": "no falsifiable claim",
                "checked_at": now}
    # strongest claim first; run only it
    res = run_claim(claims[0], cwd=skill_dir, timeout=int(cfg["falsify_run_timeout_s"]))
    return {"skill": skill_dir.name, **res, "checked_at": now}


# ---------------------------------------------------------------------------
# Ledger + consequences
# ---------------------------------------------------------------------------

def _load_verdicts(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_verdicts(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _load_usage(skills_dir: Path) -> dict:
    usage_file = skills_dir / ".usage.json"
    if not usage_file.exists():
        return {}
    try:
        return json.loads(usage_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_usage(skills_dir: Path, usage: dict) -> None:
    usage_file = skills_dir / ".usage.json"
    tmp = usage_file.with_suffix(usage_file.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(usage, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, usage_file)


# @spec CP-FAL-002
def verify_all(skills_dir: Path, verdicts_path: Path, *,
               config: dict | None = None, dry_run: bool = False,
               only: str | None = None, extra_dirs: list[Path] | None = None) -> dict:
    """Verify every active skill; write the verdict ledger; return a summary.

    By default scans ``skills_dir`` (the persona's autolearn-created skills).
    ``extra_dirs`` extends the scan (e.g. ``~/.agents/skills/``) so installed
    skills with falsifiable claims can be verified too. Skills are deduped by
    name (first occurrence wins). Installed skills not tracked in ``.usage.json``
    are verified and flagged on failure but never auto-demoted (autolearn only
    manages the lifecycle of skills it created).
    """
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    usage = _load_usage(skills_dir)
    ledger = _load_verdicts(verdicts_path)
    summary = {"pass": 0, "fail": 0, "inconclusive": 0, "demoted": 0}

    seen: dict[str, Path] = {}  # name -> chosen dir
    for d in [skills_dir, *(extra_dirs or [])]:
        if not d.is_dir():
            continue
        for sd in sorted(d.iterdir()):
            if not (sd.is_dir() and (sd / "SKILL.md").exists()):
                continue
            name = sd.name
            if name not in seen:
                seen[name] = sd
            elif not claims_of(seen[name]) and claims_of(sd):
                # prefer a duplicate that actually has a falsifiable claim
                # (e.g. an installed skill with a test suite / verify: block
                # over a persona-local prose stub of the same name)
                seen[name] = sd
    skill_dirs = list(seen.values())

    for sd in skill_dirs:
        name = sd.name
        if only and name != only:
            continue
        meta = usage.get(name, {})
        if meta.get("state") == "archived":
            continue
        prev = ledger.get(name, {})
        verdict = verify_skill(sd, config=cfg)
        # maintain consecutive fail_count (resets on pass; persists on inconclusive)
        if verdict["verdict"] == "fail":
            verdict["fail_count"] = int(prev.get("fail_count", 0)) + 1
        elif verdict["verdict"] == "pass":
            verdict["fail_count"] = 0
        else:
            verdict["fail_count"] = int(prev.get("fail_count", 0))
        ledger[name] = verdict
        summary[verdict["verdict"]] += 1

    if not dry_run:
        _save_verdicts(verdicts_path, ledger)
    summary["ledger"] = ledger
    return summary


# @spec CP-FAL-004, CP-FAL-005
def apply_consequences(usage: dict, ledger: dict, *, config: dict | None = None,
                       dry_run: bool = False) -> dict:
    """Demote skills whose deterministic fail_count reached the threshold.

    Only skills already tracked in ``usage`` (autolearn-created) may be demoted.
    Pinned skills are flagged only (never demoted). Skills not in ``usage``
    (e.g. installed skills surfaced via ``--all``) are flagged only — autolearn
    does not manage their lifecycle. Probabilistic verdicts are never produced
    by this module, so nothing here acts on weak evidence.
    """
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    threshold = int(cfg["falsify_fail_demote_after"])
    out = {"demoted": [], "flagged": []}
    for name, v in ledger.items():
        if v.get("verdict") != "fail":
            continue
        if int(v.get("fail_count", 0)) < threshold:
            continue
        flag = {"skill": name, "fail_count": v["fail_count"],
                "evidence": v.get("evidence", "")[:200]}
        if name not in usage:
            # not managed by autolearn — report only, never demote/pollute usage
            out["flagged"].append(flag)
            continue
        meta = usage[name]
        pinned = bool(meta.get("pinned"))
        if pinned:
            out["flagged"].append(flag)
            continue
        if meta.get("state") != "stale":
            if not dry_run:
                meta["state"] = "stale"
            out["demoted"].append(flag)
        else:
            out["flagged"].append(flag)
    return out


# ---------------------------------------------------------------------------
# CLI handlers (thin; mirror retention.py)
# ---------------------------------------------------------------------------

def registry_for(args) -> Path:
    home = Path(os.environ.get("AUTOLEARN_HOME", Path.home() / ".autolearn"))
    persona = getattr(args, "persona", None) or "default"
    return home / "personas" / persona


def cmd_falsify_run(args):
    persona_dir = registry_for(args)
    skills_dir = persona_dir / "skills"
    verdicts_path = persona_dir / VERDICTS_FILE
    only = getattr(args, "id", None)
    dry = bool(getattr(args, "dry_run", False))
    scan_all = bool(getattr(args, "all", False))
    # When verifying a specific skill by name or scanning everything, also look
    # under the installed-skills dir (~/.agents/skills) — that's where skills
    # with test suites / declared verify: blocks actually live.
    agents_dir = Path(os.environ.get("AGENTS_SKILLS_DIR", Path.home() / ".agents" / "skills"))
    extra = [agents_dir] if (scan_all or only) else []
    summary = verify_all(skills_dir, verdicts_path, only=only, dry_run=dry, extra_dirs=extra)
    usage = _load_usage(skills_dir)
    cons = apply_consequences(usage, summary["ledger"], dry_run=dry)
    if not dry:
        _save_usage(skills_dir, usage)
    scope = "all (persona + installed)" if extra else "persona-only"
    print(f"Falsified skills [{scope}]: {summary['pass']} pass, {summary['fail']} fail, "
          f"{summary['inconclusive']} inconclusive.")
    if cons["demoted"]:
        print(f"Demoted to stale ({'would' if dry else 'did'}):")
        for d in cons["demoted"]:
            print(f"  {d['skill']} (fails={d['fail_count']})")
    if cons["flagged"]:
        print("Flagged (installed / pinned / already stale):")
        for d in cons["flagged"]:
            print(f"  {d['skill']} (fails={d['fail_count']})")


def cmd_falsify_verdicts(args):
    persona_dir = registry_for(args)
    ledger = _load_verdicts(persona_dir / VERDICTS_FILE)
    if not ledger:
        print("No verdicts yet. Run `autolearn falsify run` first.")
        return
    for name in sorted(ledger):
        v = ledger[name]
        print(f"  {name}: {v['verdict']} ({v.get('method', '?')}) "
              f"fails={v.get('fail_count', 0)} @ {v.get('checked_at', '?')}")
