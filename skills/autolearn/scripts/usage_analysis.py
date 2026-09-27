#!/usr/bin/env python3
"""Usage-evidence scan for the skill library.

Mines agent session history (pi JSONL sessions, opencode SQLite DB, legacy
opencode storage) for ACTUAL skill loads, then reports:

- per-skill load counts and per-project distribution
- single-project concentration (project-scoping candidates)
- same-session co-occurrence pairs (umbrella/consolidation candidates)
- inventory skills never loaded in the captured window

Reads (tool calls) only count as loads when the tool input references
<skill>/SKILL.md, or when opencode's dedicated `skill` tool is invoked.
Sessions that touch >= --mgmt-threshold distinct skills in tool inputs are
classified as skills-repo maintenance sessions and excluded from per-skill
counts (they would otherwise saturate every cluster).

Stdlib only. Exit 0 always; prints the human report to stdout.

Usage:
    uv run usage_analysis.py                       # full history, human report
    uv run usage_analysis.py --days 30 --json out.json
    uv run usage_analysis.py --inventory-dir ~/github/learn-anything/.agents/skills
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

SKILL_RE = re.compile(r"([A-Za-z0-9][A-Za-z0-9_-]*)/SKILL\.md")
PI_SESSIONS = os.path.expanduser("~/.pi/agent/sessions")
OC_DB = os.path.expanduser("~/.local/share/opencode/opencode.db")
OC_STORAGE = os.path.expanduser("~/.local/share/opencode/storage")
INSTALL_DIR = os.path.expanduser("~/.agents/skills")

# ---------------------------------------------------------------------------
# session store scanning
# ---------------------------------------------------------------------------

def project_of(directory: str) -> str:
    """Collapse a working directory to a coarse project label."""
    d = (directory or "").replace("--", "/").strip("/")
    m = re.search(r"orca[-/]workspaces/(.+)", d)
    if m:
        # orca worktrees are <repo>/<slug>: the repo is the project
        d = m.group(1).split("/", 1)[0]
    if d.rstrip("/").endswith("ericmjl") or d == "":
        return "home"
    return d.split("/")[-1][:40] or "other"


def date_of(ts_ms) -> str:
    if not ts_ms:
        return "?"
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).date().isoformat()


def scan_pi_sessions() -> dict[str, dict]:
    """pi: one JSONL per session under a per-cwd directory."""
    out = {}
    for path in glob.glob(os.path.join(PI_SESSIONS, "**", "*.jsonl"), recursive=True):
        m = re.search(r"/(\d{4}-\d{2}-\d{2})T", path)
        date = m.group(1) if m else "?"
        cwd, skills = "", set()
        try:
            with open(path, errors="replace") as fh:
                for i, line in enumerate(fh):
                    if i == 0:
                        try:
                            cwd = json.loads(line).get("cwd", "")
                        except Exception:
                            pass
                    if "SKILL.md" not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if obj.get("type") != "message":
                        continue
                    content = (obj.get("message") or {}).get("content")
                    if not isinstance(content, list):
                        continue
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "toolCall":
                            skills.update(SKILL_RE.findall(json.dumps(c.get("arguments") or {})))
        except OSError:
            continue
        if skills:
            out[("pi", path)] = {"proj": project_of(cwd), "date": date, "skills": skills}
    return out


def scan_opencode_db() -> dict[str, dict]:
    """opencode v2 DB: dedicated `skill` tool + read-style tools given a path."""
    out = {}
    if not os.path.exists(OC_DB):
        return out
    con = sqlite3.connect(f"file:{OC_DB}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT id, directory, time_created FROM session_v2").fetchall()
        for sid, directory, ts in rows:
            skills = set()
            for (data,) in con.execute(
                "SELECT data FROM session_message WHERE session_id=?", (sid,)
            ):
                try:
                    obj = json.loads(data)
                except Exception:
                    continue
                content = obj.get("content")
                if not isinstance(content, list):
                    continue
                for c in content:
                    if not (isinstance(c, dict) and c.get("type") == "tool"):
                        continue
                    if c.get("name") == "skill":
                        arg = ((c.get("state") or {}).get("input") or {}).get("id")
                        if arg:
                            skills.add(arg)
                    else:
                        skills.update(
                            SKILL_RE.findall(json.dumps((c.get("state") or {}).get("input") or {}))
                        )
            if skills:
                out[("ocdb", sid)] = {
                    "proj": project_of(directory),
                    "date": date_of(ts),
                    "skills": skills,
                }
    finally:
        con.close()
    return out


def scan_opencode_legacy() -> dict[str, dict]:
    """Legacy opencode storage: session/*.json (cwd, date) + tool parts."""
    out = {}
    sess_dir = os.path.join(OC_STORAGE, "session")
    if not os.path.isdir(sess_dir):
        return out
    for path in glob.glob(os.path.join(sess_dir, "*", "*.json")):
        try:
            d = json.load(open(path))
        except Exception:
            continue
        ts = (d.get("time") or {}).get("created") or 0
        out[("oclegacy", d.get("id"))] = {
            "proj": project_of(d.get("directory")),
            "date": date_of(ts),
            "skills": set(),
        }
    part_dir = os.path.join(OC_STORAGE, "part")
    for path in glob.glob(os.path.join(part_dir, "*", "*.json")):
        try:
            with open(path, errors="replace") as fh:
                raw = fh.read()
            if "SKILL.md" not in raw:
                continue
            d = json.loads(raw)
        except Exception:
            continue
        if d.get("type") != "tool":
            continue
        sid = d.get("sessionID")
        for key in (("oclegacy", sid),):
            if key in out:
                out[key]["skills"].update(
                    SKILL_RE.findall(json.dumps((d.get("state") or {}).get("input") or {}))
                )
    return {k: v for k, v in out.items() if v["skills"]}


# ---------------------------------------------------------------------------
# inventory
# ---------------------------------------------------------------------------

def inventory_names(extra_dirs: list[str]) -> set[str]:
    names = set()
    for base in [INSTALL_DIR] + [os.path.expanduser(p) for p in extra_dirs]:
        for sk in glob.glob(os.path.join(base, "*", "SKILL.md")):
            names.add(os.path.basename(os.path.dirname(sk)))
    return names


# ---------------------------------------------------------------------------
# aggregation + report
# ---------------------------------------------------------------------------

def aggregate(sessions, inventory, mgmt_threshold, days):
    cutoff = None
    if days:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    per_skill = defaultdict(Counter)          # skill -> project -> n
    per_skill_dates = defaultdict(list)
    pairs = Counter()
    mgmt_sessions = 0
    used_sessions = 0
    for (store, _key), s in sessions.items():
        sk = s["skills"]
        if not sk:
            continue
        if cutoff and s["date"] != "?" and s["date"] < cutoff:
            continue
        if len(sk) >= mgmt_threshold:
            mgmt_sessions += 1
            continue
        used_sessions += 1
        for skill in sorted(sk):
            per_skill[skill][s["proj"]] += 1
            per_skill_dates[skill].append(s["date"])
        known = sorted(sk & inventory) or sorted(sk)
        for a in known:
            for b in known:
                if a < b:
                    pairs[(a, b)] += 1
    return per_skill, per_skill_dates, pairs, mgmt_sessions, used_sessions


def human_report(per_skill, per_skill_dates, pairs, mgmt, used, inventory, min_pair, single_share):
    lines = []
    total_by_skill = {s: sum(c.values()) for s, c in per_skill.items()}
    lines.append(f"Sessions with real skill loads: {used}  (maintenance sessions skipped: {mgmt})")
    lines.append("")
    lines.append("== Load counts by skill (project:count, most recent window) ==")
    for skill in sorted(total_by_skill, key=lambda k: -total_by_skill[k]):
        projs = per_skill[skill]
        total = total_by_skill[skill]
        dates = sorted(d for d in per_skill_dates[skill] if d != "?")
        span = f"[{dates[0]} -> {dates[-1]}]" if dates else "[?]"
        ps = ", ".join(f"{p}:{c}" for p, c in projs.most_common())
        share = max(projs.values()) / total
        flag = "  <-- single-project" if share >= single_share and total >= 2 else ""
        lines.append(f"{skill:44s} n={total:4d}  {ps}  {span}{flag}")
    never = sorted(inventory - set(per_skill))
    lines.append("")
    lines.append(f"== Inventory skills with zero loads ({len(never)}) ==")
    lines.append(", ".join(never) if never else "(none)")
    lines.append("")
    lines.append(f"== Co-occurring pairs (same session, >={min_pair}) ==")
    for (a, b), c in pairs.most_common(80):
        if c >= min_pair:
            lines.append(f"{c:4d}  {a} + {b}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=0, help="only count sessions from the last N days (0 = all)")
    ap.add_argument("--json", metavar="PATH", help="also write the full result as JSON")
    ap.add_argument("--inventory-dir", action="append", default=[],
                    help="extra dir of project-local skills to include in the inventory (repeatable)")
    ap.add_argument("--min-pair", type=int, default=3, help="min sessions for a co-occurrence pair to be reported")
    ap.add_argument("--mgmt-threshold", type=int, default=10,
                    help="sessions touching >= N distinct skills count as maintenance")
    ap.add_argument("--single-share", type=float, default=0.8,
                    help="share of loads in one project to flag single-project skills")
    args = ap.parse_args()

    sessions = {}
    sessions.update(scan_opencode_legacy())
    sessions.update(scan_opencode_db())
    sessions.update(scan_pi_sessions())
    inventory = inventory_names(args.inventory_dir)

    per_skill, per_skill_dates, pairs, mgmt, used = aggregate(
        sessions, inventory, args.mgmt_threshold, args.days
    )

    if args.json:
        payload = {
            "generated": datetime.now(timezone.utc).isoformat(),
            "inventory_size": len(inventory),
            "sessions_with_loads": used,
            "maintenance_sessions_skipped": mgmt,
            "skills": {
                s: {
                    "total": sum(c.values()),
                    "by_project": dict(c),
                    "first": min((d for d in per_skill_dates[s] if d != "?"), default=None),
                    "last": max((d for d in per_skill_dates[s] if d != "?"), default=None),
                }
                for s, c in sorted(per_skill.items())
            },
            "never_loaded": sorted(inventory - set(per_skill)),
            "pairs": {f"{a} + {b}": c for (a, b), c in pairs.items() if c >= args.min_pair},
        }
        with open(os.path.expanduser(args.json), "w") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)

    print(human_report(per_skill, per_skill_dates, pairs, mgmt, used,
                       inventory, args.min_pair, args.single_share))


if __name__ == "__main__":
    main()
