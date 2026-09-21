"""Shortcuts — Loop 2 of Certified Procedures. Detect roundabout workflows;
extract the golden path; gate promotion on falsification.

Schema's efficiency gain came from "plan inside the certified model for free":
pay the discovery cost once, then reuse. The autolearn analog is: the agent
spends tokens rediscovering a CLI invocation each session; capture the direct
("golden") command once and reuse it. This module detects that rediscovery from
recorded tool-call sequences, and hands candidates to the existing skill/memory
capture path — but only after ``falsify`` confirms the direct command still
works (CP-SHO-003), so lucky one-off commands don't harden into bad shortcuts.

Two roundabout shapes are detected:
  * ``help-chain`` — >= N consecutive --help/-h probes before a working command.
  * ``error-run``  — >= M consecutive error tool calls before a completed call
                     with the same tool.

Cost is measured in real tokens from the ``step_cost`` ledger (the data the FTS5
index throws away).

Spec: docs/designs/certified-procedures/LLD.md, certified-procedures-EARS.md (CP-SHO-*).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import falsify
import outcomes

DEFAULT_CONFIG = {
    "roundabout_help_depth": 2,
    "roundabout_error_run": 3,
    "roundabout_recent_sessions": 50,
    "shortcut_promote_min_tokens": 2000,
    "shortcut_verify_timeout_s": 120,
}

CANDIDATES_FILE = "shortcuts.json"


# ---------------------------------------------------------------------------
# Sequence helpers
# ---------------------------------------------------------------------------

def _command_of(row: dict) -> str | None:
    try:
        inp = json.loads(row.get("input_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        inp = {}
    cmd = inp.get("command")
    return cmd if isinstance(cmd, str) else None


def _first_word(command: str | None) -> str | None:
    if not command:
        return None
    return command.strip().split()[0] if command.strip() else None


def _is_help_probe(row: dict) -> bool:
    if row.get("tool") != "bash":
        return False
    cmd = (_command_of(row) or "").lower()
    if "--help" in cmd:
        return True
    # bare "foo -h" but not "foo -help-ish"
    return bool(re.search(r"(^|\s)-h(\s|$)", cmd))


def _is_error(row: dict) -> bool:
    return row.get("status") == "error"


# @spec CP-SHO-002
def _help_chains(session_id: str, rows: list[dict], config: dict) -> list[dict]:
    """Help-chains: >= N consecutive --help probes. The golden command is the
    first *completed* call after the run that shares a first token with a probe
    (else None — a pure-cost finding, never promoted)."""
    depth = int(config["roundabout_help_depth"])
    out: list[dict] = []
    i, n = 0, len(rows)
    while i < n:
        if _is_help_probe(rows[i]):
            j = i
            while j < n and _is_help_probe(rows[j]):
                j += 1
            run_len = j - i
            if run_len >= depth:
                probe_firsts = {_first_word(_command_of(r)) for r in rows[i:j]}
                probe_firsts.discard(None)
                golden = None
                gtool = "bash"
                end_seq = int(rows[j - 1]["seq"])
                if j < n and rows[j].get("status") == "completed":
                    gcmd = _command_of(rows[j])
                    if gcmd and _first_word(gcmd) in probe_firsts:
                        golden = gcmd
                        gtool = rows[j].get("tool", "bash")
                        end_seq = int(rows[j]["seq"])
                out.append({
                    "session_id": session_id, "kind": "help-chain",
                    "start_seq": int(rows[i]["seq"]), "end_seq": end_seq,
                    "tool": gtool, "golden_command": golden,
                    "run_length": run_len,
                })
            i = j
        else:
            i += 1
    return out


# @spec CP-SHO-002
def _error_runs(session_id: str, rows: list[dict], config: dict) -> list[dict]:
    """Error-runs: >= M consecutive error calls. The golden command is the first
    *completed* call after the run with the same tool (else None)."""
    run = int(config["roundabout_error_run"])
    out: list[dict] = []
    i, n = 0, len(rows)
    while i < n:
        if _is_error(rows[i]):
            j = i
            while j < n and _is_error(rows[j]):
                j += 1
            run_len = j - i
            if run_len >= run:
                err_tool = rows[i].get("tool")
                golden = None
                gtool = err_tool
                end_seq = int(rows[j - 1]["seq"])
                if (j < n and rows[j].get("status") == "completed"
                        and rows[j].get("tool") == err_tool):
                    golden = _command_of(rows[j])
                    gtool = rows[j].get("tool", err_tool)
                    end_seq = int(rows[j]["seq"])
                out.append({
                    "session_id": session_id, "kind": "error-run",
                    "start_seq": int(rows[i]["seq"]), "end_seq": end_seq,
                    "tool": gtool, "golden_command": golden,
                    "run_length": run_len,
                })
            i = j
        else:
            i += 1
    return out


# @spec CP-SHO-001
def session_token_cost(index: outcomes.OutcomeIndex, session_id: str,
                       start_seq: int, end_seq: int) -> int:
    """Tokens (in+out+reasoning) spent on step_cost rows between two seq markers."""
    row = index.conn.execute(
        "SELECT COALESCE(SUM(tokens_in + tokens_out + tokens_reasoning), 0) AS s "
        "FROM step_cost WHERE session_id = ? AND seq BETWEEN ? AND ?",
        [session_id, start_seq, end_seq],
    ).fetchone()
    return int(row["s"]) if row else 0


# @spec CP-SHO-001
def detect_roundabouts(index: outcomes.OutcomeIndex, *, config: dict | None = None) -> list[dict]:
    """Scan recent sessions for help-chain and error-run roundabouts.

    Each candidate carries ``cost_tokens`` (the wasted discovery spend) and
    ``golden_command`` (the direct invocation that worked, or None).
    """
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    recent = int(cfg["roundabout_recent_sessions"])
    cands: list[dict] = []
    for sid in index.recent_sessions(recent):
        rows = index.session_sequences(sid)
        found = _help_chains(sid, rows, cfg) + _error_runs(sid, rows, cfg)
        for c in found:
            c["cost_tokens"] = session_token_cost(index, sid, c["start_seq"], c["end_seq"])
            cands.append(c)
    # most expensive first
    cands.sort(key=lambda c: c.get("cost_tokens", 0), reverse=True)
    return cands


# @spec CP-SHO-003 (promotion gated by verification)
def verify_candidate(candidate: dict, *, config: dict | None = None,
                     cwd: Path | None = None) -> dict:
    """Deterministically verify a candidate's golden command before promotion.

    Builds a declared claim from ``golden_command`` and runs it through
    ``falsify.run_claim`` (safe-subset enforced). Candidates whose golden
    command is unsafe or absent come back inconclusive and are not promoted.
    """
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    cmd = candidate.get("golden_command")
    if not cmd:
        return {"verdict": "inconclusive", "reason": "no golden command"}
    claim = {"method": "declared", "command": cmd, "expect_exit": 0}
    return falsify.run_claim(claim, cwd=cwd or Path.cwd(),
                             timeout=int(cfg["shortcut_verify_timeout_s"]))


# @spec CP-SHO-004
def promotable(candidates: list[dict], *, config: dict | None = None) -> list[dict]:
    """Filter to candidates worth surfacing for promotion (min token savings)."""
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    floor = int(cfg["shortcut_promote_min_tokens"])
    return [c for c in candidates
            if c.get("golden_command") and int(c.get("cost_tokens", 0)) >= floor]


# ---------------------------------------------------------------------------
# Persistence + CLI
# ---------------------------------------------------------------------------

def _load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save(path: Path, cands: list[dict]) -> None:
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(cands, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def registry_for(args) -> Path:
    home = Path(os.environ.get("AUTOLEARN_HOME", Path.home() / ".autolearn"))
    persona = getattr(args, "persona", None) or "default"
    return home / "personas" / persona


def cmd_shortcuts_detect(args):
    persona_dir = registry_for(args)
    idx = outcomes.OutcomeIndex(persona_dir / outcomes.OUTCOMES_DB_NAME,
                                outcomes._opencode_db_path())
    idx.init_schema()
    cands = detect_roundabouts(idx)
    dry = bool(getattr(args, "dry_run", False))
    prom = promotable(cands)
    if not dry:
        _save(persona_dir / CANDIDATES_FILE, cands)
    print(f"Detected {len(cands)} roundabout candidates "
          f"({len(prom)} worth promoting, >= {DEFAULT_CONFIG['shortcut_promote_min_tokens']} tok).")
    for c in cands[:15]:
        cmd = (c.get("golden_command") or "<none>")[:70]
        print(f"  [{c['kind']}] {c['cost_tokens']} tok  tool={c['tool']}  golden={cmd}")
    idx.close()


def cmd_shortcuts_list(args):
    persona_dir = registry_for(args)
    cands = _load(persona_dir / CANDIDATES_FILE)
    if not cands:
        print("No shortcut candidates. Run `autolearn shortcuts detect` first.")
        return
    prom = promotable(cands)
    print(f"{len(cands)} candidates ({len(prom)} promotable):")
    for c in cands:
        mark = "*" if c in prom else " "
        cmd = (c.get("golden_command") or "<none>")[:70]
        print(f" {mark} [{c['kind']}] {c['cost_tokens']} tok  tool={c['tool']}  golden={cmd}")
