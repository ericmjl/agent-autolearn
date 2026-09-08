"""Wiki layer — persistent, compounding knowledge between memory and skills.

The persona-local wiki/ directory holds one pattern page per diagnosed
problem (trigger, root cause, resolution, evidence), an index catalog, an
evolution log, and the harness-written skill-impact ledger. Adapted from
WikiSkill (arXiv 2608.27454): skills are hypotheses (gated, rollback-able);
the wiki is evidence (append, consolidate, never roll back).

Embedding-free (lexical match over index one-liners), stdlib only.

Spec: docs/designs/wiki-layer/LLD.md, wiki-layer-EARS.md (WIKI-PAT-*, WIKI-IMP-*).
"""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime, date
from pathlib import Path

DEFAULT_CONFIG = {
    "wiki_enabled": True,
    "wiki_page_max_lines": 100,
    "wiki_context_recent_log_lines": 40,
    "wiki_context_ledger_entries": 20,
}

INDEX_HEADER = (
    "# Wiki Pattern Index\n\n"
    "<!-- Managed by autolearn wiki layer. One line per pattern: "
    "PROBLEM + root cause + fix. -->\n\n"
)

LEDGER_HEADER = (
    "# Skill Impact Ledger\n\n"
    "<!-- APPEND-ONLY, harness-written. One JSON line per skill mutation "
    "or verdict. Rejected attempts stay forever: never re-propose a "
    "rejected approach. -->\n\n"
)

LOG_HEADER = (
    "# Wiki Evolution Log\n\n"
    "<!-- Chronological log of review findings and wiki actions. -->\n\n"
)

SECTION_TITLES = {
    "trigger": "## Trigger",
    "root_cause": "## Root Cause",
    "resolution": "## Resolution",
    "evidence": "## Evidence",
}


# ---------------------------------------------------------------------------
# Paths & enable gate
# ---------------------------------------------------------------------------

def wiki_dir(persona_dir: Path) -> Path:
    return Path(persona_dir) / "wiki"


def patterns_dir(persona_dir: Path) -> Path:
    return wiki_dir(persona_dir) / "patterns"


def consolidated_dir(persona_dir: Path) -> Path:
    return wiki_dir(persona_dir) / "consolidated"


def pattern_path(persona_dir: Path, slug: str) -> Path:
    return patterns_dir(persona_dir) / f"{slug}.md"


# @spec WIKI-PAT-007
def enabled(persona_dir: Path) -> bool:
    """Read wiki_enabled from config.yaml; default True. Never fatal."""
    cfg = Path(persona_dir) / "config.yaml"
    try:
        import yaml  # pyyaml is already a dependency of autolearn.py
        data = yaml.safe_load(cfg.read_text()) or {}
    except Exception:
        return True
    return bool(data.get("wiki_enabled", True))


def _page_limit(persona_dir: Path) -> int:
    cfg = Path(persona_dir) / "config.yaml"
    try:
        import yaml
        data = yaml.safe_load(cfg.read_text()) or {}
    except Exception:
        return DEFAULT_CONFIG["wiki_page_max_lines"]
    return int(data.get("wiki_page_max_lines", DEFAULT_CONFIG["wiki_page_max_lines"]))


# ---------------------------------------------------------------------------
# Scaffold
# ---------------------------------------------------------------------------

# @spec WIKI-PAT-001
def ensure_scaffold(persona_dir: Path) -> Path:
    """Create wiki/ scaffold idempotently. Never overwrites existing files."""
    d = wiki_dir(persona_dir)
    d.mkdir(parents=True, exist_ok=True)
    patterns_dir(persona_dir).mkdir(parents=True, exist_ok=True)
    consolidated_dir(persona_dir).mkdir(parents=True, exist_ok=True)
    for name, header in (
        ("index.md", INDEX_HEADER),
        ("logs.md", LOG_HEADER),
        ("skill-impact.md", LEDGER_HEADER),
    ):
        f = d / name
        if not f.exists():
            f.write_text(header, encoding="utf-8")
    return d


# ---------------------------------------------------------------------------
# Pattern pages
# ---------------------------------------------------------------------------

def _render_pattern(title: str, trigger: str, root_cause: str,
                    resolution: str, evidence: list[str]) -> str:
    today = date.today().isoformat()
    ev_lines = "\n".join(f"- {s}" for s in evidence) if evidence else "- (none yet)"
    return (
        f"# {title}\n\n"
        f"{trigger}\n\n"
        f"{SECTION_TITLES['root_cause']}\n\n{root_cause}\n\n"
        f"{SECTION_TITLES['resolution']}\n\n{resolution}\n\n"
        f"{SECTION_TITLES['evidence']}\n\n{ev_lines}\n\n"
        f"Updated: {today}\n"
    )


def _check_line_cap(content: str, limit: int, slug: str) -> None:
    n = len(content.splitlines())
    if n > limit:
        raise ValueError(
            f"pattern page '{slug}' would be {n} lines; cap is {limit}. "
            "Consolidate the page (merge older evidence, shorten) before appending."
        )


def _bump_updated(content: str) -> str:
    """Refresh the trailing 'Updated: YYYY-MM-DD' line."""
    content = re.sub(r"^Updated: .*$", "", content, flags=re.MULTILINE)
    return content.rstrip() + f"\n\nUpdated: {date.today().isoformat()}\n"


# @spec WIKI-PAT-002
def write_pattern(persona_dir: Path, slug: str, title: str, body: str,
                  *, session_ids: list[str] | None = None) -> Path:
    """Create a new pattern page; refuses if over the line cap.

    ``body`` is the page body the caller supplies (the reviewer composes
    trigger/root-cause/resolution); ``evidence`` session IDs are appended
    under '## Evidence' if not already present.
    """
    ensure_scaffold(persona_dir)
    if not slug or "/" in slug or slug.startswith("."):
        raise ValueError(f"invalid pattern slug: {slug!r}")
    path = pattern_path(persona_dir, slug)
    if path.exists():
        raise FileExistsError(
            f"pattern '{slug}' already exists at {path}; use update_pattern()"
        )
    ev = list(session_ids or [])
    if ev:
        body = body.rstrip() + f"\n\n{SECTION_TITLES['evidence']}\n\n" + \
            "\n".join(f"- {s}" for s in ev) + "\n"
    if not body.lstrip().startswith("# "):
        body = f"# {title}\n\n" + body
    body += f"\nUpdated: {date.today().isoformat()}\n"
    _check_line_cap(body, _page_limit(persona_dir), slug)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, path)
    return path


# @spec WIKI-PAT-003
def update_pattern(persona_dir: Path, slug: str, *, add_resolution: str | None = None,
                   add_evidence: list[str] | None = None) -> Path:
    """Insert new evidence/resolution into an existing page (append semantics).

    - add_evidence: appends lines under '## Evidence' (creates the section
      if absent).
    - add_resolution: appends a dated line under '## Resolution'.
    Refreshes 'Updated'. Refuses if the page would exceed the line cap.
    """
    path = pattern_path(persona_dir, slug)
    if not path.exists():
        raise FileNotFoundError(f"pattern '{slug}' not found at {path}")
    content = path.read_text(encoding="utf-8")
    additions: list[str] = []
    today = date.today().isoformat()

    if add_resolution:
        additions.append(f"- {today}: {add_resolution}")
    if add_evidence:
        additions.extend(f"- {s}" for s in add_evidence if s not in content)

    if additions:
        if SECTION_TITLES["evidence"] in content:
            marker = SECTION_TITLES["evidence"]
            idx = content.index(marker) + len(marker)
            content = content[:idx] + "\n" + "\n".join(additions) + content[idx:].lstrip("\n")
        else:
            content = content.rstrip() + "\n\n" + SECTION_TITLES["evidence"] + "\n\n" + \
                "\n".join(additions) + "\n"
        content = _bump_updated(content)

    _check_line_cap(content, _page_limit(persona_dir), slug)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------

# @spec WIKI-PAT-004
def upsert_index_entry(persona_dir: Path, slug: str, one_liner: str) -> None:
    """Keep index.md at exactly one line per pattern."""
    ensure_scaffold(persona_dir)
    idx = wiki_dir(persona_dir) / "index.md"
    lines = idx.read_text(encoding="utf-8").splitlines()
    entry = f"- [{slug}](patterns/{slug}.md): {one_liner}"
    lines = [ln for ln in lines if not ln.startswith(f"- [{slug}](patterns/")]
    # drop consolidated tombstones of this slug too
    lines = [ln for ln in lines if slug not in ln or ln.startswith("- [")]
    if entry not in lines:
        lines.append(entry)
    tmp = idx.with_suffix(idx.suffix + f".{os.getpid()}.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, idx)


def remove_index_entry(persona_dir: Path, slug: str) -> None:
    idx = wiki_dir(persona_dir) / "index.md"
    if not idx.exists():
        return
    lines = [ln for ln in idx.read_text(encoding="utf-8").splitlines()
             if not ln.startswith(f"- [{slug}](patterns/")]
    tmp = idx.with_suffix(idx.suffix + f".{os.getpid()}.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, idx)


# ---------------------------------------------------------------------------
# Logs + ledger
# ---------------------------------------------------------------------------

def _append_line(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(text.rstrip() + "\n")


# @spec WIKI-IMP-001, WIKI-IMP-002, WIKI-IMP-003
def append_skill_impact(persona_dir: Path, *, action: str, skill: str,
                        summary: str, outcome: str, trigger: str) -> None:
    """Append one ledger line. HARNESS-ONLY: callers are autolearn.py code
    paths (skill create/patch/archive, falsify demote, proposer promote),
    never the reviewer agent."""
    assert action in {"create", "patch", "demote", "archive", "promote"}
    assert outcome in {"accepted", "rejected", "demoted", "archived"}
    assert trigger in {"review", "falsify", "proposer", "curator"}
    ensure_scaffold(persona_dir)
    entry = {
        "date": datetime.now().isoformat(timespec="seconds"),
        "action": action,
        "skill": skill,
        "summary": summary[:300],
        "outcome": outcome,
        "trigger": trigger,
    }
    import json
    _append_line(wiki_dir(persona_dir) / "skill-impact.md", json.dumps(entry, ensure_ascii=False))


def read_ledger(persona_dir: Path, last_n: int = 20) -> list[dict]:
    f = wiki_dir(persona_dir) / "skill-impact.md"
    if not f.exists():
        return []
    entries: list[dict] = []
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            entries.append(__import__("json").loads(line))
        except Exception:
            continue
    return entries[-last_n:]


def rejected_approaches(persona_dir: Path) -> list[str]:
    """Summaries of ledger entries with outcome in {rejected, demoted}."""
    return [e.get("summary", "") for e in read_ledger(persona_dir, last_n=500)
            if e.get("outcome") in {"rejected", "demoted"}]


# @spec WIKI-PAT-006
def append_log(persona_dir: Path, *, summary: str, counts: dict | None = None) -> None:
    ensure_scaffold(persona_dir)
    today = date.today().isoformat()
    line = f"- {today}: {summary}"
    if counts:
        bits = " ".join(f"{k}={v}" for k, v in counts.items() if v)
        if bits:
            line += f" ({bits})"
    _append_line(wiki_dir(persona_dir) / "logs.md", line)


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

def tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) >= 2}


# @spec WIKI-PAT-005
def find_relevant_patterns(persona_dir: Path, query: str) -> list[dict]:
    """Lexical match over index.md one-liners; ranked by match count."""
    idx = wiki_dir(persona_dir) / "index.md"
    if not idx.exists():
        return []
    q = tokens(query)
    if not q:
        return []
    out: list[dict] = []
    for ln in idx.read_text(encoding="utf-8").splitlines():
        if not ln.startswith("- ["):
            continue
        try:
            slug = ln.split("[", 1)[1].split("]", 1)[0]
        except IndexError:
            continue
        score = len(q & tokens(ln))
        if score > 0:
            out.append({"slug": slug, "line": ln, "score": score})
    out.sort(key=lambda d: d["score"], reverse=True)
    return out


def list_patterns(persona_dir: Path) -> list[dict]:
    """All pattern pages with title + Updated date."""
    pdir = patterns_dir(persona_dir)
    if not pdir.exists():
        return []
    out = []
    for f in sorted(pdir.glob("*.md")):
        text = f.read_text(encoding="utf-8", errors="replace")
        title = text.splitlines()[0].lstrip("# ").strip() if text.startswith("#") else f.stem
        m = re.search(r"Updated: (\S+)", text)
        out.append({"slug": f.stem, "title": title,
                    "updated": m.group(1) if m else "unknown",
                    "lines": len(text.splitlines())})
    return out


# ---------------------------------------------------------------------------
# Composed reviewer view (wiki/context.md)
# ---------------------------------------------------------------------------

# @spec WIKI-PAT-006
def compose_context(persona_dir: Path, config: dict | None = None) -> str:
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    d = wiki_dir(persona_dir)
    parts: list[str] = []

    idx = d / "index.md"
    parts.append("## Pattern Index\n")
    parts.append(idx.read_text(encoding="utf-8") if idx.exists()
                 else "(no patterns yet)\n")

    logf = d / "logs.md"
    parts.append("\n## Recent Evolution Log\n")
    if logf.exists():
        lines = [ln for ln in logf.read_text(encoding="utf-8").splitlines()
                 if ln.startswith("- ")]
        parts.append("\n".join(lines[-cfg.get("wiki_context_recent_log_lines",
                                                 40):]) + "\n" if lines else "(empty)\n")
    else:
        parts.append("(empty)\n")

    parts.append("\n## Recent Skill Impact (rejected approaches: DO NOT repeat)\n")
    entries = read_ledger(persona_dir, last_n=cfg.get("wiki_context_ledger_entries", 20))
    if entries:
        for e in entries:
            parts.append(f"- [{e.get('date', '?')}] {e.get('action')} {e.get('skill', '?')}"
                         f" -> {e.get('outcome', '?')}: {e.get('summary', '')}\n")
    else:
        parts.append("(no skill mutations recorded yet)\n")

    parts.append(
        "\nOpen specific pattern pages with: "
        "`autolearn.py wiki show <slug>` — read pages on demand, do not assume.\n"
    )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# PURPOSE.md provenance
# ---------------------------------------------------------------------------

def write_purpose(skill_dir: Path, *, origin: str, patterns: list[str],
                  evolution: str) -> Path:
    """Create or update PURPOSE.md in a persona-local skill directory."""
    skill_dir = Path(skill_dir)
    skill_dir.mkdir(parents=True, exist_ok=True)
    p = skill_dir / "PURPOSE.md"
    pats = "\n".join(f"- [{s}](../../wiki/patterns/{s}.md)" for s in patterns) or "- (none)"
    if not p.exists():
        p.write_text(
            "# Purpose\n\n"
            f"## Origin\n\n{origin}\n\n"
            f"## Patterns Addressed\n\n{pats}\n\n"
            f"## Evolution History\n\n- {date.today().isoformat()}: created\n",
            encoding="utf-8",
        )
    else:
        # WIKI-SKL-002: append a dated evolution line
        with p.open("a", encoding="utf-8") as fh:
            fh.write(f"- {date.today().isoformat()}: {evolution or 'patched'}\n")
    return p


# ---------------------------------------------------------------------------
# CLI handlers
# ---------------------------------------------------------------------------

def _persona_dir() -> Path:
    import autolearn  # late import to avoid a cycle in tests
    return autolearn.ACTIVE_PERSONA_DIR


def cmd_wiki_init(args):
    d = ensure_scaffold(_persona_dir())
    print(f"Wiki scaffold ready: {d}")
    backfilled = backfill_purposes()
    if backfilled:
        print(f"PURPOSE.md backfilled for {len(backfilled)} skill(s): "
              + ", ".join(backfilled))


def cmd_wiki_list(args):
    pats = list_patterns(_persona_dir())
    if not pats:
        print("No pattern pages yet. The reviewer writes them when a "
              "diagnosed pattern has not recurred enough for a skill.")
        return
    for p in pats:
        print(f"  {p['slug']} [{p['updated']}] {p['lines']} lines — {p['title']}")


def cmd_wiki_show(args):
    path = pattern_path(_persona_dir(), args.slug)
    if not path.exists():
        print(f"Pattern not found: {args.slug}")
        sys.exit(1)
    print(path.read_text(encoding="utf-8"))


def cmd_wiki_read(args):
    hits = find_relevant_patterns(_persona_dir(), args.terms)
    if not hits:
        print("No matching patterns.")
        return
    for h in hits:
        print(f"  ({h['score']}) {h['line']}")


def cmd_wiki_compose(args):
    md = compose_context(_persona_dir())
    out = wiki_dir(_persona_dir()) / "context.md"
    tmp = out.with_suffix(out.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(md, encoding="utf-8")
    os.replace(tmp, out)
    print(f"Wrote {out} ({len(md)} chars)")


def cmd_logs_append(args):
    append_log(_persona_dir(), summary=args.summary,
               counts=None)
    print("Logged.")


def cmd_impact_append(args):
    # Internal/debug helper — mirrors the harness append signature.
    append_skill_impact(_persona_dir(), action=args.action, skill=args.skill,
                        summary=args.summary, outcome=args.outcome,
                        trigger=args.trigger)
    print("Ledger entry appended.")


def cmd_wiki_backfill(args):
    done = backfill_purposes()
    if not done:
        print("All persona-local skills already have PURPOSE.md (or none exist).")
    else:
        print("Backfilled: " + ", ".join(done))


# @spec WIKI-SKL-003
def backfill_purposes() -> list[str]:
    """Create PURPOSE.md for persona-local skills missing one.

    Provenance source order: proposals.json promoted_skill records ->
    ledger 'create' entries -> explicit unknown placeholder. Never touches
    skills outside the persona dir (hand-authored skills in ~/.agents/skills).
    """
    import autolearn
    import json
    done: list[str] = []
    if not autolearn.SKILLS_DIR.exists():
        return done
    # source 1: proposals
    promoted: dict[str, str] = {}
    try:
        proposals = proposer_load(autolearn.ACTIVE_PERSONA_DIR)
        for p in proposals.values():
            name = p.get("promoted_skill")
            if name and p.get("request_summary"):
                promoted.setdefault(name, p["request_summary"])
    except Exception:
        pass
    # source 2: ledger
    for e in read_ledger(autolearn.ACTIVE_PERSONA_DIR, last_n=10000):
        if e.get("action") == "create" and e.get("outcome") == "accepted":
            promoted.setdefault(e.get("skill", ""), e.get("summary", ""))
    for skill_dir in sorted(autolearn.SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir() or skill_dir.name.startswith("."):
            continue
        usage = autolearn.load_usage()
        meta = usage.get(skill_dir.name)
        if not meta or meta.get("created_by") != "autolearn":
            continue  # hand-authored or unmanaged: never touched
        p = skill_dir / "PURPOSE.md"
        if p.exists():
            continue
        origin = promoted.get(skill_dir.name) or \
            "origin: created before the wiki layer; provenance unknown"
        desc = ""
        sm = skill_dir / "SKILL.md"
        if sm.exists():
            lines = sm.read_text(encoding="utf-8").splitlines()
            for i, ln in enumerate(lines):
                if ln.startswith("description: |"):
                    desc = "\n".join(
                        l.strip() for l in lines[i + 1:] if l.startswith("  ")
                    ).strip()
                    break
        origin_full = f"{desc}\n\n{origin}" if desc and desc != origin else origin
        write_purpose(skill_dir, origin=origin_full, patterns=[],
                      evolution="backfilled")
        done.append(skill_dir.name)
    return done


def proposer_load(persona_dir: Path) -> dict:
    import proposer
    return proposer._load(persona_dir)