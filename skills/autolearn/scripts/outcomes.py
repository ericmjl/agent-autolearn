"""Outcome index — the shared spine for Certified Procedures.

Indexes ``opencode.db``'s ``part`` rows that the FTS5 search index deliberately
excludes (session-search DD4): ``tool`` parts (command -> outcome), ``step-finish``
parts (per-step token cost), and ``skill`` tool parts (skill-load -> session
linkage). Writes to a persona-local ``outcomes.db`` (never to ``opencode.db``).

Two loops consume this spine:
  * ``falsify.py`` (Loop 1) — verify skills against ground-truth outcomes.
  * ``shortcuts.py`` (Loop 2) — detect roundabout tool sequences + golden paths.

Critical epistemic rule: an outcome only *falsifies* where it carries a
ground-truth bit (test result > exit code > user correction > raw output). Raw
structured output (grep/read/edit) is never above gt_strength 1.

Spec: docs/designs/certified-procedures/LLD.md, certified-procedures-EARS.md (CP-OUT-*).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

OUTCOMES_DB_NAME = "outcomes.db"
OUTPUT_PEEK_CHARS = 400

DEFAULT_CONFIG = {
    "outcomes_index_batch": 5000,
}

# Test-runner cues used by gt_strength classification (CP-OUT-003).
TEST_RUNNERS = (
    "pytest", "tox", "nox", "unittest", "npm test", "npm run test",
    "yarn test", "pnpm test", "vitest", "jest", "mocha",
    "go test", "cargo test", "rake test", "mix test", "dotnet test",
    "gradle test", "mvn test", "pixi run test",
)
# Commands permitted for a *declared* falsification claim (CP-FAL-003). A
# declared claim must be a test-runner invocation; arbitrary code execution
# (`python -c`, `os.system`, network, destructive verbs) is refused and the
# claim comes back inconclusive rather than executed.
SAFE_DECLARED_TOKENS = TEST_RUNNERS
SAFE_DECLARED_DENY = re.compile(
    r"\b(rm|sudo|curl|wget|nc|ssh|scp|dd|mkfs|chmod\s|[>]&1|"
    r"os\.system|subprocess|eval\(|exec\(|__import__|urllib|requests|socket|"
    r"python[0-9.]*\s+-c)\b"
)


def _json(s: str | None) -> dict:
    if not s:
        return {}
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return {}


def _peek(s: str | None, n: int = OUTPUT_PEEK_CHARS) -> str:
    if not s:
        return ""
    return s[:n]


def is_test_command(tool: str, input_json: dict, output_peek: str) -> bool:
    """Heuristic: does this tool call invoke a test runner?

    Checks the *command* only (not output) so a non-test command whose output
    happens to mention a runner is not misclassified.
    """
    if tool != "bash":
        return False
    cmd = input_json.get("command") if isinstance(input_json.get("command"), str) else ""
    return any(t in cmd for t in TEST_RUNNERS)


def exit_code_of(tool: str, status: str, input_json: dict, output_peek: str,
                 meta_exit: int | None = None) -> int | None:
    """Best-effort exit code. Prefers ``state.metadata.exit`` (where opencode
    actually records it for bash); falls back to status/text cues."""
    if meta_exit is not None:
        return int(meta_exit)
    if status != "completed":
        return 1 if status == "error" else None
    if tool != "bash":
        return None
    blob = f"{input_json.get('command','')} {output_peek}".lower()
    for cue in ("exit code ", "exited with ", "exit: "):
        i = blob.find(cue)
        if i >= 0:
            tail = blob[i + len(cue):].lstrip(" -:")
            sign = 1
            j = 0
            if tail[:1] in "-":
                sign = -1
                j = 1
            num = ""
            while j < len(tail) and tail[j].isdigit():
                num += tail[j]
                j += 1
            if num:
                return sign * int(num)
    return None


# @spec CP-OUT-003
def classify_gt(tool: str, status: str, input_json: dict, output_peek: str,
                *, meta_exit: int | None = None) -> int:
    """Ground-truth strength: 4 test | 3 exit-code | 2 correction | 1 raw | 0 none.

    Note: ``2 correction`` (a user text correction within a window after the
    call) is RESERVED for the deferred probabilistic layer and is NOT assigned
    here — linking tool calls to subsequent user corrections requires text-part
    windowing this index does not perform. This function therefore caps
    tool-derived strength at 3. See EARS CP-OUT-003 + LLD.
    """
    if tool == "bash":
        if is_test_command(tool, input_json, output_peek):
            return 4
        if meta_exit is not None or exit_code_of(tool, status, input_json,
                                                 output_peek, meta_exit) is not None:
            return 3
        return 1
    if status == "error":
        return 3
    if tool in ("read", "edit", "write", "glob", "grep"):
        return 1
    if tool == "skill":
        return 0  # a skill load is linkage, not an outcome
    return 1 if output_peek else 0


class OutcomeIndex:
    """A queryable index over opencode.db tool-call + step-cost parts.

    Persona-local: ``outcomes.db`` lives under the persona dir and is never
    synced (opencode.db is machine-local by nature; verdict provenance is
    machine-local, not canonical).
    """

    def __init__(self, outcomes_db: Path, opencode_db: Path):
        self.outcomes_db = Path(outcomes_db)
        self.opencode_db = Path(opencode_db)
        self.outcomes_db.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.outcomes_db))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.init_schema()  # idempotent; guarantees query methods work pre-index

    def init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS index_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tool_outcome (
                part_id      TEXT PRIMARY KEY,
                session_id   TEXT NOT NULL,
                message_id   TEXT NOT NULL,
                seq          INTEGER NOT NULL,
                time_created INTEGER NOT NULL,
                tool         TEXT NOT NULL,
                status       TEXT NOT NULL,
                skill_name   TEXT,
                input_json   TEXT,
                output_peek  TEXT,
                gt_strength  INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS to_session_seq ON tool_outcome (session_id, seq);
            CREATE INDEX IF NOT EXISTS to_skill ON tool_outcome (skill_name);
            CREATE INDEX IF NOT EXISTS to_tool_status ON tool_outcome (tool, status);

            CREATE TABLE IF NOT EXISTS step_cost (
                part_id      TEXT PRIMARY KEY,
                session_id   TEXT NOT NULL,
                seq          INTEGER NOT NULL,
                time_created INTEGER NOT NULL,
                reason       TEXT,
                tokens_in    INTEGER NOT NULL DEFAULT 0,
                tokens_out   INTEGER NOT NULL DEFAULT 0,
                tokens_reasoning INTEGER NOT NULL DEFAULT 0,
                cache_read   INTEGER NOT NULL DEFAULT 0,
                cache_write  INTEGER NOT NULL DEFAULT 0,
                cost         REAL NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS sc_session_seq ON step_cost (session_id, seq);
            """
        )
        self.conn.commit()

    def _last_mark(self) -> tuple[int, str]:
        """Return (last_time, last_part_id) — a composite key so parts sharing
        a millisecond timestamp are not skipped across runs."""
        row = self.conn.execute(
            "SELECT value FROM index_state WHERE key = 'last_part_time'"
        ).fetchone()
        if not row:
            return (0, "")
        t, _, pid = str(row["value"]).partition("|")
        try:
            return (int(t), pid)
        except ValueError:
            return (0, "")

    def _set_mark(self, ts: int, pid: str) -> None:
        self.conn.execute(
            "INSERT INTO index_state(key, value) VALUES('last_part_time', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [f"{ts}|{pid}"],
        )

    def _opencode_conn(self) -> sqlite3.Connection | None:
        if not self.opencode_db.exists():
            return None
        c = sqlite3.connect(f"file:{self.opencode_db}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        return c

    # @spec CP-OUT-001, CP-OUT-002
    def index(self, *, full: bool = False, config: dict | None = None) -> dict:
        """Incremental high-water-mark index of opencode.db part rows.

        Returns counters: {tool_parts, step_parts, skill_loads, gt_ge2, skipped, errors}.
        """
        cfg = {**DEFAULT_CONFIG, **(config or {})}
        self.init_schema()

        # Hoist the source-DB check ABOVE any --full truncation so a missing/
        # moved opencode.db can never wipe an existing index (data-loss guard).
        db = self._opencode_conn()
        if db is None:
            return {"tool_parts": 0, "step_parts": 0, "skill_loads": 0,
                    "gt_ge2": 0, "skipped": 0, "errors": 0, "no_opencode_db": True}

        if full:
            self.conn.execute("DELETE FROM tool_outcome")
            self.conn.execute("DELETE FROM step_cost")
            self.conn.execute("DELETE FROM index_state WHERE key = 'last_part_time'")
            self.conn.commit()

        last_time, last_id = self._last_mark()
        batch = int(cfg["outcomes_index_batch"])
        summary = {"tool_parts": 0, "step_parts": 0, "skill_loads": 0,
                   "gt_ge2": 0, "skipped": 0, "errors": 0}
        high_time, high_id = last_time, last_id

        rows = db.execute(
            "SELECT p.id AS part_id, p.session_id, p.message_id, "
            "p.time_created, p.data AS data "
            "FROM part p "
            "WHERE p.time_created > ? OR (p.time_created = ? AND p.id > ?) "
            "ORDER BY p.time_created ASC, p.id ASC",
            [last_time, last_time, last_id],
        )

        tool_rows: list[tuple] = []
        step_rows: list[tuple] = []

        for r in rows:
            data = _json(r["data"])
            ptype = data.get("type")
            ts = int(r["time_created"])
            pid = r["part_id"]
            # cursor is ordered (time, id) ASC, so each row advances the mark
            high_time, high_id = ts, pid
            session_id = r["session_id"]
            message_id = r["message_id"]
            if ptype == "step-finish":
                tokens = data.get("tokens") or {}
                step_rows.append((
                    pid, session_id, ts, ts, data.get("reason"),
                    int(tokens.get("input") or 0), int(tokens.get("output") or 0),
                    int(tokens.get("reasoning") or 0),
                    int((tokens.get("cache") or {}).get("read") or 0),
                    int((tokens.get("cache") or {}).get("write") or 0),
                    float(data.get("cost") or 0.0),
                ))
                summary["step_parts"] += 1
            elif ptype == "tool":
                state = data.get("state") or {}
                status = state.get("status") or "completed"
                inp = state.get("input") or {}
                inp_json = json.dumps(inp, ensure_ascii=False)
                metadata = state.get("metadata") or {}
                meta_exit = metadata.get("exit")
                out = state.get("output")
                peek = _peek(out if isinstance(out, str) else json.dumps(out, ensure_ascii=False))
                tool = data.get("tool") or "unknown"
                skill_name = inp.get("name") if tool == "skill" else None
                gt = classify_gt(tool, status, inp, peek, meta_exit=meta_exit)
                tool_rows.append((
                    pid, session_id, message_id, ts, ts, tool, status,
                    skill_name, inp_json, peek, gt,
                ))
                summary["tool_parts"] += 1
                if tool == "skill":
                    summary["skill_loads"] += 1
                if gt >= 2:
                    summary["gt_ge2"] += 1
            else:
                summary["skipped"] += 1

            if len(tool_rows) + len(step_rows) >= batch:
                self._flush(tool_rows, step_rows)
                self._set_mark(high_time, high_id)
                tool_rows, step_rows = [], []

        self._flush(tool_rows, step_rows)
        self._set_mark(high_time, high_id)
        db.close()
        return summary

    def _flush(self, tool_rows: list[tuple], step_rows: list[tuple]) -> None:
        if tool_rows:
            self.conn.executemany(
                "INSERT OR REPLACE INTO tool_outcome "
                "(part_id, session_id, message_id, seq, time_created, tool, status, "
                " skill_name, input_json, output_peek, gt_strength) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                tool_rows,
            )
        if step_rows:
            self.conn.executemany(
                "INSERT OR REPLACE INTO step_cost "
                "(part_id, session_id, seq, time_created, reason, tokens_in, tokens_out, "
                " tokens_reasoning, cache_read, cache_write, cost) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                step_rows,
            )
        self.conn.commit()

    # -- query API (data-returning; CP-OUT-004) ---------------------------

    def list_tool_outcomes(self, *, session_id: str | None = None,
                           tool: str | None = None, skill: str | None = None,
                           min_gt: int = 0, limit: int | None = None) -> list[dict]:
        sql = ("SELECT part_id, session_id, message_id, seq, time_created, tool, "
               "status, skill_name, input_json, output_peek, gt_strength "
               "FROM tool_outcome WHERE 1=1")
        params: list = []
        if session_id is not None:
            sql += " AND session_id = ?"
            params.append(session_id)
        if tool is not None:
            sql += " AND tool = ?"
            params.append(tool)
        if skill is not None:
            sql += " AND skill_name = ?"
            params.append(skill)
        if min_gt:
            sql += " AND gt_strength >= ?"
            params.append(min_gt)
        sql += " ORDER BY session_id, seq, part_id"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return [dict(r) for r in self.conn.execute(sql, params)]

    def session_sequences(self, session_id: str) -> list[dict]:
        """Ordered tool_outcome rows for a session (for roundabout detection)."""
        return self.list_tool_outcomes(session_id=session_id)

    def recent_sessions(self, limit: int = 50) -> list[str]:
        rows = self.conn.execute(
            "SELECT session_id FROM tool_outcome "
            "GROUP BY session_id "
            "ORDER BY MAX(time_created) DESC LIMIT ?",
            [limit],
        ).fetchall()
        return [r["session_id"] for r in rows]

    # @spec CP-OUT-005
    def skill_use_counts(self) -> dict[str, int]:
        """{skill_name: load_count} derived from tool='skill' part counts.

        Authoritative source for repairing .usage.json use_count.
        """
        rows = self.conn.execute(
            "SELECT skill_name, COUNT(*) AS c FROM tool_outcome "
            "WHERE tool='skill' AND skill_name IS NOT NULL "
            "GROUP BY skill_name"
        ).fetchall()
        return {r["skill_name"]: int(r["c"]) for r in rows}

    # @spec CP-OUT-005, SM-LC-013, SM-LC-014
    def skill_use_signals(self) -> dict[str, dict]:
        """``{skill_name: {"count": int, "last_seen_ms": int}}`` derived from ``tool='skill'`` parts.

        Extends :meth:`skill_use_counts` with the most recent load timestamp per
        skill (``MAX(time_created)``). The timestamp feeds ``last_activity_at``
        when ``repair_skill_use_counts()`` adds ``tracked-manual`` entries for
        previously-untracked on-disk skills (SM-LC-014).

        ``last_seen_ms`` is ``0`` when the outcome table has no rows for the
        skill (defensive — should not happen since the row only exists when
        there is at least one load).
        """
        rows = self.conn.execute(
            "SELECT skill_name, COUNT(*) AS c, MAX(time_created) AS last_ms "
            "FROM tool_outcome "
            "WHERE tool='skill' AND skill_name IS NOT NULL "
            "GROUP BY skill_name"
        ).fetchall()
        return {
            r["skill_name"]: {
                "count": int(r["c"]),
                "last_seen_ms": int(r["last_ms"]) if r["last_ms"] is not None else 0,
            }
            for r in rows
        }

    def status(self) -> dict:
        def count(sql: str) -> int:
            return int(self.conn.execute(sql).fetchone()[0])
        tool_total = count("SELECT COUNT(*) FROM tool_outcome")
        step_total = count("SELECT COUNT(*) FROM step_cost")
        gt_hist = {str(r["gt"]): int(r["c"]) for r in self.conn.execute(
            "SELECT gt_strength AS gt, COUNT(*) AS c FROM tool_outcome GROUP BY gt_strength"
        ).fetchall()}
        last, _ = self._last_mark()
        last_iso = datetime.fromtimestamp(last / 1000).strftime("%Y-%m-%d %H:%M") if last else "never"
        return {
            "tool_parts": tool_total,
            "step_parts": step_total,
            "skill_loads": count("SELECT COUNT(*) FROM tool_outcome WHERE tool='skill'"),
            "gt_histogram": gt_hist,
            "last_indexed": last_iso,
            "outcomes_db": str(self.outcomes_db),
        }

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:
            pass


def registry_for(args) -> Path:
    """Resolve the persona dir for a CLI invocation (mirrors retention.registry_for)."""
    home = Path(os.environ.get("AUTOLEARN_HOME", Path.home() / ".autolearn"))
    persona = getattr(args, "persona", None) or "default"
    return home / "personas" / persona


def _opencode_db_path() -> Path:
    return Path.home() / ".local" / "share" / "opencode" / "opencode.db"


def cmd_outcomes_init(args):
    persona_dir = registry_for(args)
    idx = OutcomeIndex(persona_dir / OUTCOMES_DB_NAME, _opencode_db_path())
    if getattr(args, "full", False):
        print("Clearing existing outcome index for full rebuild...")
    summary = idx.index(full=bool(getattr(args, "full", False)))
    if summary.get("no_opencode_db"):
        print(f"OpenCode database not found at {_opencode_db_path()}")
        idx.close()
        raise SystemExit(1)
    print(f"Indexed {summary['tool_parts']} tool parts, "
          f"{summary['step_parts']} step-cost parts "
          f"({summary['skill_loads']} skill loads).")
    print(f"Ground-truth outcomes (gt>=2): {summary['gt_ge2']}")
    idx.close()


def cmd_outcomes_status(args):
    persona_dir = registry_for(args)
    idx = OutcomeIndex(persona_dir / OUTCOMES_DB_NAME, _opencode_db_path())
    s = idx.status()
    print(f"Outcome index: {s['outcomes_db']}")
    print(f"  Tool parts: {s['tool_parts']}")
    print(f"  Step-cost parts: {s['step_parts']}")
    print(f"  Skill loads: {s['skill_loads']}")
    print(f"  Ground-truth histogram: {s['gt_histogram']}")
    print(f"  Last indexed: {s['last_indexed']}")
    idx.close()
