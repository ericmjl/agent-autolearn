"""Long-horizon skill proposer — cross-session recurrence → staged proposals.

Replaces reactive-per-session skill creation with evidence-driven-cross-session
synthesis: study many sessions, cluster by user-request + resolution, and stage
a *proposal* when a cluster recurs across >= M sessions. A proposal
auto-promotes to a skill only when its common resolution passes falsification
(deterministic verification). The reviewer's ``skill create`` is hard-gated on
``is_recurrent`` so myopic per-session creation can no longer happen.

Reuses the Certified Procedures substrate: ``outcomes`` (tool calls + recent
sessions), ``shortcuts`` (golden commands), ``falsify`` (verify the resolution),
``shift`` (topic signatures). Opens ``search.db`` read-only for user-message
text. No ``autolearn.py`` import.

Spec: docs/designs/long-horizon-skills/LLD.md.
"""
from __future__ import annotations

import json
import hashlib
import os
import sqlite3
from datetime import date
from pathlib import Path

import falsify
import outcomes
import shift
from shift import topic_signature

DEFAULT_CONFIG = {
    "proposer_recent_sessions": 50,
    "proposer_min_sessions": 3,
    "proposer_verify_timeout_s": 60,
}

PROPOSALS_FILE = "proposals.json"
SEARCH_DB_NAME = "search.db"
MIN_SUBSTANTIVE_LEN = 20


# ---------------------------------------------------------------------------
# Persona / DB resolution
# ---------------------------------------------------------------------------

def registry_for(args) -> Path:
    home = Path(os.environ.get("AUTOLEARN_HOME", Path.home() / ".autolearn"))
    persona = getattr(args, "persona", None) or "default"
    return home / "personas" / persona


def _persona_dir_from(persona_dir: Path) -> Path:
    return Path(persona_dir)


def _search_conn(persona_dir: Path) -> sqlite3.Connection | None:
    db = persona_dir / SEARCH_DB_NAME
    if not db.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _outcomes_idx(persona_dir: Path) -> outcomes.OutcomeIndex:
    return outcomes.OutcomeIndex(persona_dir / outcomes.OUTCOMES_DB_NAME,
                                 outcomes._opencode_db_path())


# ---------------------------------------------------------------------------
# Per-session signal extraction
# ---------------------------------------------------------------------------

def _session_user_messages(search_conn: sqlite3.Connection, session_id: str) -> list[str]:
    try:
        rows = search_conn.execute(
            "SELECT text FROM session_text_content WHERE session_id = ? AND role = 'user' "
            "ORDER BY timestamp",
            [session_id],
        ).fetchall()
    except sqlite3.Error:
        return []
    return [r["text"] for r in rows if r["text"]]


def _first_substantive(messages: list[str]) -> str | None:
    for m in messages:
        s = (m or "").strip()
        if len(s) >= MIN_SUBSTANTIVE_LEN and topic_signature(s)[0]:
            return s
    return None


def _richest_user_message(messages: list[str]) -> str | None:
    """Pick the user message with the most topic tokens (the strongest request signal).

    Real sessions often start with a short utterance; the richest message is the
    best proxy for 'what was this session about'.
    """
    best, best_n = None, 0
    for m in messages:
        s = (m or "").strip()
        if len(s) < MIN_SUBSTANTIVE_LEN:
            continue
        n = len(topic_signature(s)[1])
        if n > best_n:
            best, best_n = s, n
    return best


def _stem(tok: str) -> str:
    """Light plural normalization (test/tests, run/runs). Deliberately tiny."""
    if len(tok) > 3 and tok.endswith("s"):
        return tok[:-1]
    return tok


def _stemmed_tokens(text: str) -> set[str]:
    _sig, tokens = topic_signature(text or "")
    return {_stem(t) for t in tokens}


def _session_request(search_conn, session_id) -> tuple[set[str], str] | None:
    """Return (stemmed topic tokens, summary) for the session's richest user message."""
    msgs = _session_user_messages(search_conn, session_id)
    sub = _richest_user_message(msgs)
    if not sub:
        return None
    tokens = _stemmed_tokens(sub)
    if len(tokens) < 2:
        return None
    summary = sub[:120].replace("\n", " ").strip()
    return (tokens, summary)


def _overlap_coefficient(a: set[str], b: set[str]) -> float:
    """|A∩B| / min(|A|,|B|). Better than Jaccard for short-vs-long request matches."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


SIMILARITY_THRESHOLD = 0.5  # overlap-coefficient threshold to count as same request


TRIVIAL_FIRST_TOKENS = {"echo", "ls", "cat", "pwd", "cd", "true", "false",
                        "export", "printf", "test", "which", "file"}


def _session_resolution(idx: outcomes.OutcomeIndex, session_id: str) -> str | None:
    """The last *meaningful* successful bash command (skip trivial diagnostics).

    Walks the session's bash outcomes newest→oldest and returns the first
    completed command whose first token isn't a trivial diagnostic (echo/ls/cat/…).
    """
    rows = idx.list_tool_outcomes(session_id=session_id, tool="bash")
    for r in reversed(rows):
        if r.get("status") != "completed":
            continue
        try:
            inp = json.loads(r.get("input_json") or "{}")
        except json.JSONDecodeError:
            continue
        cmd = inp.get("command")
        if not (isinstance(cmd, str) and cmd.strip()):
            continue
        first = cmd.strip().split()[0]
        if first in TRIVIAL_FIRST_TOKENS:
            continue
        return cmd.strip()
    return None


# ---------------------------------------------------------------------------
# Proposal store
# ---------------------------------------------------------------------------

def _load(persona_dir: Path) -> dict:
    p = persona_dir / PROPOSALS_FILE
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save(persona_dir: Path, proposals: dict) -> None:
    p = persona_dir / PROPOSALS_FILE
    tmp = p.with_suffix(p.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(proposals, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def _proposal_id(sig: str) -> str:
    return sig[:16] or "unknown"


def _modal_command(commands: list[str]) -> str | None:
    """Most frequent command in a cluster (ties: first). None if empty."""
    if not commands:
        return None
    counts: dict[str, int] = {}
    for c in commands:
        counts[c] = counts.get(c, 0) + 1
    return max(commands, key=lambda c: counts[c])


# ---------------------------------------------------------------------------
# Scan / verify / promote
# ---------------------------------------------------------------------------

# @spec LH-PROP-001
def scan(persona_dir: Path, *, config: dict | None = None) -> dict:
    """Cluster recent sessions by request_signature; refresh proposals.json.

    Returns {sessions_scanned, proposals_total, new, updated}. Promotes nothing.
    """
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    persona_dir = _persona_dir_from(persona_dir)
    search_conn = _search_conn(persona_dir)
    idx = _outcomes_idx(persona_dir)
    proposals = _load(persona_dir)

    sessions = idx.recent_sessions(int(cfg["proposer_recent_sessions"]))
    # Per-session (topic_tokens_set, summary, resolution, session_id)
    per_session: list[tuple[set, str, str | None, str]] = []
    scanned = 0
    for sid in sessions:
        req = _session_request(search_conn, sid) if search_conn else None
        res = _session_resolution(idx, sid)
        if not req:
            continue
        scanned += 1
        tokens, summary = req
        per_session.append((set(tokens), summary, res, sid))

    # Greedy single-linkage clustering: a session joins a cluster if it is
    # similar (>= SIMILARITY_THRESHOLD overlap) to ANY member. Single-linkage
    # groups varied wordings of the same request that a single-seed compare
    # would miss.
    clusters: list[list[tuple[set, str, str | None, str]]] = []
    for entry in per_session:
        tokens = entry[0]
        placed = False
        for cluster in clusters:
            if any(_overlap_coefficient(tokens, m[0]) >= SIMILARITY_THRESHOLD for m in cluster):
                cluster.append(entry)
                placed = True
                break
        if not placed:
            clusters.append([entry])

    min_sessions = int(cfg["proposer_min_sessions"])
    today = date.today().isoformat()
    new = updated = 0
    for cluster in clusters:
        if len(cluster) < min_sessions:
            continue
        # proposal id = stable hash of the seed's sorted tokens
        seed_tokens_sorted = sorted(cluster[0][0])
        pid = hashlib.sha1("|".join(seed_tokens_sorted).encode()).hexdigest()[:16]
        resolutions = [m[2] for m in cluster if m[2]]
        summaries = [m[1] for m in cluster]
        session_ids = [m[3] for m in cluster]
        existing = proposals.get(pid)
        rec = {
            "id": pid,
            "request_signature": pid,
            "request_summary": summaries[0] if summaries else "",
            "common_resolution": _modal_command(resolutions),
            "session_ids": session_ids,
            "sessions_count": len(cluster),
            "est_tokens_saved": 0,
            "first_seen": today,
            "last_seen": today,
            "status": existing.get("status", "pending") if existing else "pending",
            "verified": existing.get("verified") if existing else None,
            "promoted_skill": existing.get("promoted_skill") if existing else None,
            "updated_at": today,
        }
        if existing:
            rec["first_seen"] = existing.get("first_seen", today)
            updated += 1
        else:
            new += 1
        proposals[pid] = rec

    _save(persona_dir, proposals)
    if search_conn is not None:
        search_conn.close()
    idx.close()
    return {"sessions_scanned": scanned, "proposals_total": len(proposals),
            "new": new, "updated": updated}


# @spec LH-PROP-002
def verify_pending(persona_dir: Path, *, config: dict | None = None) -> dict:
    """Run falsify on each *unverified* pending proposal's common_resolution.

    Only proposals with ``verified is None`` are processed, so frequent scans
    stay cheap (known-pass proposals are promoted; known-fail proposals are not
    re-run). verified = True only on a clean pass; False on fail; null on
    inconclusive / unsafe (stays pending). Auto-promotion consumes verified=True.
    """
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    proposals = _load(persona_dir)
    checked = 0
    for pid, p in proposals.items():
        if p.get("status") != "pending":
            continue
        if p.get("verified") is not None:
            continue  # already verified — skip (keeps frequent scans cheap)
        cmd = p.get("common_resolution")
        if not cmd:
            p["verified"] = None
            continue
        checked += 1
        claim = {"method": "declared", "command": cmd, "expect_exit": 0}
        res = falsify.run_claim(claim, cwd=Path(persona_dir),
                                timeout=int(cfg["proposer_verify_timeout_s"]))
        v = res.get("verdict")
        p["verified"] = True if v == "pass" else (False if v == "fail" else None)
        p["verify_evidence"] = res.get("evidence", "")
    _save(persona_dir, proposals)
    return {"checked": checked, "verified": sum(1 for p in proposals.values() if p.get("verified") is True)}


# @spec LH-PROP-003
def promote_ready(persona_dir: Path) -> list[dict]:
    """Pending proposals with verified == True — candidates for auto-promotion."""
    proposals = _load(persona_dir)
    return [p for p in proposals.values()
            if p.get("status") == "pending" and p.get("verified") is True]


# @spec LH-PROP-004
def is_recurrent(persona_dir: Path, request_text: str, *, config: dict | None = None) -> dict:
    """The reviewer's hard-gate check: does this request recur across >= min_sessions?

    Uses Jaccard similarity (>= SIMILARITY_THRESHOLD) so varied wordings of the
    same request count as a recurrence. Fail-safe: if search.db is missing or
    unreadable, returns recurrent=False (the reviewer records a memory instead
    rather than being blocked on a missing index).
    """
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    target_set = _stemmed_tokens(request_text)
    if len(target_set) < 2:
        return {"recurrent": False, "sessions_count": 0, "reason": "no signature"}
    search_conn = _search_conn(persona_dir)
    if search_conn is None:
        return {"recurrent": False, "sessions_count": 0, "reason": "no search index"}
    try:
        rows = search_conn.execute(
            "SELECT DISTINCT session_id FROM session_text_content WHERE role = 'user'"
        ).fetchall()
    except sqlite3.Error:
        search_conn.close()
        return {"recurrent": False, "sessions_count": 0, "reason": "search schema unreadable"}
    count = 0
    for r in rows:
        sid = r["session_id"]
        msgs = _session_user_messages(search_conn, sid)
        hit = False
        for m in msgs:
            tokens = _stemmed_tokens(m or "")
            if len(tokens) >= 2 and _overlap_coefficient(target_set, tokens) >= SIMILARITY_THRESHOLD:
                hit = True
                break
        if hit:
            count += 1
    search_conn.close()
    return {"recurrent": count >= int(cfg["proposer_min_sessions"]),
            "sessions_count": count}


# ---------------------------------------------------------------------------
# CLI handlers
# ---------------------------------------------------------------------------

def cmd_proposals_list(args):
    persona_dir = registry_for(args)
    proposals = _load(persona_dir)
    if not proposals:
        print("No proposals. Run `autolearn proposals scan` first.")
        return
    pending = [p for p in proposals.values() if p.get("status") == "pending"]
    print(f"{len(pending)} pending / {len(proposals)} total proposals:")
    for p in sorted(pending, key=lambda x: -x.get("sessions_count", 0)):
        verified = p.get("verified")
        vmark = "verified" if verified is True else ("failed" if verified is False else "unverified")
        cmd = (p.get("common_resolution") or "<none>")[:70]
        print(f"  [{p['id']}] {p['sessions_count']} sessions, {vmark}: {cmd}")
        print(f"      request: {p.get('request_summary','')[:80]}")


def cmd_proposals_scan(args):
    persona_dir = registry_for(args)
    summary = scan(persona_dir)
    print(f"Scanned {summary['sessions_scanned']} sessions: "
          f"{summary['new']} new proposals, {summary['updated']} updated "
          f"({summary['proposals_total']} total).")
    res = verify_pending(persona_dir)
    print(f"Verified {res['checked']} proposals: {res['verified']} passed (auto-promote-ready).")


def cmd_proposals_recurrence(args):
    persona_dir = registry_for(args)
    text = getattr(args, "text", "") or ""
    res = is_recurrent(persona_dir, text)
    print(f"recurrent={'true' if res['recurrent'] else 'false'} "
          f"sessions_count={res['sessions_count']}")


def cmd_proposals_confirm(args):
    """Manually force-promote a proposal (bypasses verification)."""
    persona_dir = registry_for(args)
    proposals = _load(persona_dir)
    pid = getattr(args, "id", "")
    p = proposals.get(pid)
    if not p:
        print(f"No proposal {pid}")
        return
    p["status"] = "promoted"
    p["verified"] = True
    _save(persona_dir, proposals)
    print(f"Proposal {pid} marked promoted (manual). Create the skill via `skill create`.")


def cmd_proposals_dismiss(args):
    persona_dir = registry_for(args)
    proposals = _load(persona_dir)
    pid = getattr(args, "id", "")
    p = proposals.get(pid)
    if not p:
        print(f"No proposal {pid}")
        return
    p["status"] = "dismissed"
    _save(persona_dir, proposals)
    print(f"Proposal {pid} dismissed.")
