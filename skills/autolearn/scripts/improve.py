"""Self-improving agent CLI — manages ~/.agent-improvement/rules.yaml.

Commands:
  observe   Record a new observation (rule + project + context)
  status    Show all rules, counts, escalation status
  due       Show rules at escalation threshold but not yet written
  escalate  Print or --apply rules to AGENTS.md files
  confirm   Mark an escalation as applied after writing
  history   Show full observation history for a rule
  domains   List domain taxonomy
  stale     Show rules not reinforced in N days (--days, default 90)
  seed      Import existing rules from an AGENTS.md file
  init      Initialize the rules.yaml file
"""

# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml", "filelock"]
# ///
from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import yaml
from filelock import FileLock

DATA_HOME = Path(
    os.environ.get("AGENT_IMPROVEMENT_HOME", Path.home() / ".agent-improvement")
)
RULES_FILE = DATA_HOME / "rules.yaml"
LOCK_FILE = DATA_HOME / "rules.yaml.lock"

DEFAULT_DOMAIN_TAXONOMONY = {
    "general": [
        "python-tooling",
        "git-practices",
        "security",
        "code-style",
        "error-handling",
        "testing",
        "documentation",
        "communication",
        "tool-usage",
        "search-patterns",
    ],
    "project_specific": [
        "auth-architecture",
        "import-patterns",
        "file-structure",
        "api-design",
        "database-queries",
        "ui-components",
        "config-management",
        "deployment",
    ],
}

DEFAULT_RULES = {
    "version": 1,
    "domain_taxonomy": DEFAULT_DOMAIN_TAXONOMONY,
    "rules": [],
}

DOMAIN_TO_SECTION = {
    "python-tooling": "Python Tool Management",
    "git-practices": "Git Practices",
    "security": "Security",
    "code-style": "Code Style",
    "error-handling": "Error Handling",
    "testing": "Testing",
    "documentation": "Documentation",
    "communication": "Communication Preferences",
    "tool-usage": "Tool Usage",
    "search-patterns": "Search Patterns",
    "workflow": "Workflow",
    "data-viz": "Data Visualization",
    "architecture": "Architecture",
    "data-quality": "Data Quality",
    "data-strategy": "Data Strategy",
    "data-sources": "Data Sources",
    "data-patterns": "Data Patterns",
    "auth-architecture": "Auth Architecture",
    "import-patterns": "Import Patterns",
    "file-structure": "File Structure",
    "api-design": "API Design",
    "database-queries": "Database Queries",
    "ui-components": "UI Components",
    "config-management": "Config Management",
    "deployment": "Deployment",
}


def _ensure_init():
    DATA_HOME.mkdir(parents=True, exist_ok=True)
    if not RULES_FILE.exists():
        _write_yaml(DEFAULT_RULES)


def _read_yaml() -> dict:
    if not RULES_FILE.exists():
        return dict(DEFAULT_RULES)
    with open(RULES_FILE) as f:
        data = yaml.safe_load(f)
    return data if data else dict(DEFAULT_RULES)


def _write_yaml(data: dict):
    DATA_HOME.mkdir(parents=True, exist_ok=True)
    with open(RULES_FILE, "w") as f:
        yaml.dump(
            data, f, default_flow_style=False, sort_keys=False, allow_unicode=True
        )


def _with_lock(fn):
    def wrapper(*args, **kwargs):
        DATA_HOME.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(LOCK_FILE), timeout=10)
        with lock:
            return fn(*args, **kwargs)

    return wrapper


def _slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"[\s]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text[:80].rstrip("-")


def _rule_id(rule_text: str) -> str:
    return _slugify(rule_text)


def _get_domain_scope(domain: str, taxonomy: dict) -> str:
    for scope, domains in taxonomy.items():
        if domain in domains:
            return scope
    return "unknown"


def _detect_project() -> str:
    try:
        remote = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if remote.returncode == 0:
            url = remote.stdout.strip()
            name = url.rstrip("/").split("/")[-1]
            return name.replace(".git", "")
    except Exception:
        pass
    return os.environ.get("AGENT_PROJECT_NAME", "unknown")


def _agents_md_path(scope: str) -> Path:
    """Return the AGENTS.md file path for the given scope."""
    if scope == "global":
        return Path(os.environ.get("AGENT_GLOBAL_AGENTS_MD", Path.home() / ".config" / "opencode" / "AGENTS.md"))
    return Path(os.environ.get("AGENT_PROJECT_AGENTS_MD", "AGENTS.md"))


def _apply_to_agents_md(rule_text: str, domain: str, scope: str) -> str | None:
    """Patch an AGENTS.md file by inserting the rule under the right section.

    Returns the file path written, or None if the rule was a duplicate.
    Creates the file with a header if it doesn't exist.
    """
    path = _agents_md_path(scope)
    path.parent.mkdir(parents=True, exist_ok=True)

    content = ""
    if path.exists():
        content = path.read_text()

    rule_line = f"- {rule_text}"

    # Duplicate check: skip if rule text already present
    if rule_text in content:
        return None

    section = DOMAIN_TO_SECTION.get(domain, "Agent Rules")
    header = f"## {section}"

    if not content:
        # Create new file with header + section + rule
        title = "# Global OpenCode Rules" if scope == "global" else "# Project Agent Rules"
        content = f"{title}\n\n{header}\n\n{rule_line}\n"
        path.write_text(content)
        return str(path)

    if header not in content:
        # Append new section at end
        if not content.endswith("\n"):
            content += "\n"
        content += f"\n{header}\n\n{rule_line}\n"
        path.write_text(content)
        return str(path)

    # Insert rule under existing section — find section end
    lines = content.split("\n")
    insert_idx = None
    section_start = None
    for i, line in enumerate(lines):
        if line.strip() == header:
            section_start = i
            continue
        if section_start is not None and line.startswith("## ") and line.strip() != header:
            insert_idx = i
            break
    if insert_idx is None:
        insert_idx = len(lines)

    # Find last non-empty line before insert_idx
    while insert_idx > 0 and lines[insert_idx - 1].strip() == "":
        insert_idx -= 1

    lines.insert(insert_idx, rule_line)
    path.write_text("\n".join(lines))
    return str(path)


@_with_lock
def cmd_init(args):
    if RULES_FILE.exists() and not args.force:
        print(f"rules.yaml already exists at {RULES_FILE}. Use --force to overwrite.")
        sys.exit(1)
    _ensure_init()
    print(f"Initialized {RULES_FILE}")


@_with_lock
def cmd_observe(args):
    data = _read_yaml()
    taxonomy = data.get("domain_taxonomy", DEFAULT_DOMAIN_TAXONOMONY)
    rule_text = args.rule
    rid = _rule_id(rule_text)
    project = args.project or _detect_project()
    domain = args.domain or "unknown"
    today = date.today().isoformat()
    explicit_scope = getattr(args, "explicit_scope", None)

    existing = None
    for r in data["rules"]:
        if r["id"] == rid:
            existing = r
            break

    observation = {
        "project": project,
        "date": today,
        "context": args.context or rule_text,
    }

    if existing:
        existing["observations"].append(observation)
        existing["total_count"] = len(existing["observations"])
        existing["last_reinforced"] = today
        projects_seen = {o["project"] for o in existing["observations"]}
        existing["project_count"] = len(projects_seen)
        existing["cross_project"] = len(projects_seen) > 1
        if explicit_scope:
            existing["explicit_scope"] = explicit_scope
        if domain != "unknown" and existing.get("domain", "unknown") == "unknown":
            existing["domain"] = domain
    else:
        existing = {
            "id": rid,
            "rule": rule_text,
            "domain": domain,
            "created": today,
            "last_reinforced": today,
            "observations": [observation],
            "cross_project": False,
            "total_count": 1,
            "project_count": 1,
            "escalation": None,
            "escalated_at": None,
            "written_to": [],
        }
        if explicit_scope:
            existing["explicit_scope"] = explicit_scope
        data["rules"].append(existing)

    _write_yaml(data)

    count = existing["total_count"]
    cross = existing["cross_project"]
    scope = _get_domain_scope(existing["domain"], taxonomy)
    exp = existing.get("explicit_scope")

    if exp == "global":
        print(
            f"RECORDED (count={count}, explicit=global) → should write to GLOBAL AGENTS.md"
        )
    elif exp == "local":
        print(f"RECORDED (count={count}, explicit=local) → stays in project AGENTS.md")
    elif count == 1:
        print(f"RECORDED (count=1) → apply in-session only")
    elif count >= 2 and not cross:
        print(
            f"RECORDED (count={count}, same project) → candidate for PROJECT AGENTS.md"
        )
    elif count >= 2 and cross and scope == "general":
        print(
            f"RECORDED (count={count}, cross-project, general domain) → candidate for GLOBAL AGENTS.md"
        )
    elif count >= 2 and cross and scope != "general":
        print(
            f"RECORDED (count={count}, cross-project, {scope} domain) → candidate for EACH PROJECT AGENTS.md"
        )
    else:
        print(f"RECORDED (count={count})")


@_with_lock
def cmd_status(args):
    data = _read_yaml()
    taxonomy = data.get("domain_taxonomy", DEFAULT_DOMAIN_TAXONOMONY)
    rules = data.get("rules", [])

    if not rules:
        print("No rules recorded yet.")
        return

    for r in rules:
        scope = _get_domain_scope(r["domain"], taxonomy)
        cross = "CROSS-PROJECT" if r["cross_project"] else "single-project"
        esc = r.get("escalation") or "none"
        written = len(r.get("written_to", []))
        exp = r.get("explicit_scope", "")
        exp_str = f" [explicit={exp}]" if exp else ""

        print(f"  {r['id']}")
        print(f"    rule: {r['rule']}")
        print(
            f"    count: {r['total_count']} ({cross}, domain={r['domain']} [{scope}]){exp_str}"
        )
        print(f"    escalation: {esc}, written_to: {written} files")
        print()


@_with_lock
def cmd_due(args):
    data = _read_yaml()
    taxonomy = data.get("domain_taxonomy", DEFAULT_DOMAIN_TAXONOMONY)
    rules = data.get("rules", [])
    due_rules = []

    for r in rules:
        if r.get("written_to"):
            continue
        exp = r.get("explicit_scope")
        count = r["total_count"]
        cross = r["cross_project"]
        scope = _get_domain_scope(r["domain"], taxonomy)

        if exp == "global" and count >= 1:
            due_rules.append((r, "global"))
        elif exp == "local" and count >= 2:
            due_rules.append((r, "local"))
        elif count >= 2 and not cross:
            due_rules.append((r, "local"))
        elif count >= 2 and cross and scope == "general":
            due_rules.append((r, "global"))
        elif count >= 2 and cross:
            due_rules.append((r, "local"))

    if not due_rules:
        print("No rules pending escalation.")
        return

    for r, target in due_rules:
        projects = sorted({o["project"] for o in r["observations"]})
        print(f"  {r['id']}")
        print(f"    rule: {r['rule']}")
        print(f"    count: {r['total_count']}, projects: {', '.join(projects)}")
        print(f"    → ESCALATE TO: {target} AGENTS.md")
        print()


@_with_lock
def cmd_escalate(args):
    data = _read_yaml()
    taxonomy = data.get("domain_taxonomy", DEFAULT_DOMAIN_TAXONOMONY)
    rules = data.get("rules", [])
    actions = []

    for r in rules:
        if r.get("written_to"):
            continue
        exp = r.get("explicit_scope")
        count = r["total_count"]
        cross = r["cross_project"]
        scope = _get_domain_scope(r["domain"], taxonomy)

        target = None
        if exp == "global" and count >= 1:
            target = "global"
        elif exp == "local" and count >= 2:
            target = "local"
        elif count >= 2 and not cross:
            target = "local"
        elif count >= 2 and cross and scope == "general":
            target = "global"
        elif count >= 2 and cross:
            target = "local"

        if target:
            projects = sorted({o["project"] for o in r["observations"]})
            actions.append((r, target, projects))

    if not actions:
        print("No rules to escalate.")
        return

    dry = args.dry_run
    apply = getattr(args, "apply", False)
    today = date.today().isoformat()

    for r, target, projects in actions:
        print(f"RULE: {r['rule']}")
        print(f"  TARGET: {target}")
        print(f"  PROJECTS: {', '.join(projects)}")

        if apply and not dry:
            written = _apply_to_agents_md(r["rule"], r.get("domain", "unknown"), target)
            if written:
                if not r.get("written_to"):
                    r["written_to"] = []
                r["written_to"].append({"path": written, "date": today})
                r["escalation"] = target
                r["escalated_at"] = today
                print(f"  → WROTE to {written}")
            else:
                print(f"  → SKIPPED (duplicate in target file)")
        else:
            print(f"  SUGGESTED AGENTS.md ADDITION:")
            print(f"    - {r['rule']}")
        print()

        if not dry and not apply:
            r["escalation"] = target
            r["escalated_at"] = today

    if not dry:
        _write_yaml(data)
        if apply:
            print("Rules applied to AGENTS.md and recorded.")
        else:
            print("Escalation status recorded. Run `confirm` after writing to AGENTS.md.")


@_with_lock
def cmd_confirm(args):
    data = _read_yaml()
    rid = args.rule_id
    scope = args.scope
    today = date.today().isoformat()

    if scope == "global":
        path = str(_agents_md_path("global"))
    else:
        path = str(_agents_md_path("local"))

    for r in data["rules"]:
        if r["id"] == rid:
            if not r.get("written_to"):
                r["written_to"] = []
            r["written_to"].append({"path": path, "date": today})
            r["escalation"] = scope
            _write_yaml(data)
            print(f"Confirmed: {rid} written to {path}")
            return

    print(f"Rule not found: {rid}")
    sys.exit(1)


@_with_lock
def cmd_history(args):
    data = _read_yaml()
    rid = args.rule_id

    for r in data["rules"]:
        if r["id"] == rid:
            print(f"Rule: {r['rule']}")
            print(f"Domain: {r['domain']}")
            print(f"Created: {r['created']}")
            print(f"Total count: {r['total_count']}")
            print(f"Cross-project: {r['cross_project']}")
            print(f"Project count: {r['project_count']}")
            print()
            print("Observations:")
            for obs in r["observations"]:
                print(f"  [{obs['date']}] {obs['project']}: {obs['context']}")
            print()
            written = r.get("written_to", [])
            if written:
                print("Written to:")
                for w in written:
                    print(f"  {w['path']} ({w['date']})")
            else:
                print("Not yet written to any AGENTS.md.")
            return

    print(f"Rule not found: {rid}")
    sys.exit(1)


@_with_lock
def cmd_domains(args):
    data = _read_yaml()
    taxonomy = data.get("domain_taxonomy", DEFAULT_DOMAIN_TAXONOMONY)
    for scope, domains in taxonomy.items():
        print(f"{scope}:")
        for d in domains:
            print(f"  - {d}")
        print()


@_with_lock
def cmd_stale(args):
    """Show rules not reinforced in N days."""
    data = _read_yaml()
    rules = data.get("rules", [])
    days = args.days
    cutoff = date.today() - timedelta(days=days)
    stale_rules = []

    for r in rules:
        last = r.get("last_reinforced") or r.get("created", "2000-01-01")
        try:
            last_date = date.fromisoformat(last)
        except (ValueError, TypeError):
            last_date = date.fromisoformat("2000-01-01")
        if last_date < cutoff:
            stale_rules.append((r, last))

    if not stale_rules:
        print(f"No stale rules (all reinforced within {days} days).")
        return

    print(f"STALE RULES (not reinforced in {days}+ days):\n")
    for r, last in sorted(stale_rules, key=lambda x: x[1]):
        written = "WRITTEN" if r.get("written_to") else "NOT WRITTEN"
        print(f"  {r['id']}")
        print(f"    rule: {r['rule']}")
        print(f"    last_reinforced: {last}  [{written}]")
        print(f"    count: {r['total_count']}, domain: {r.get('domain', 'unknown')}")
        if r.get("written_to"):
            for w in r["written_to"]:
                print(f"    → {w['path']}")
        print()


@_with_lock
def cmd_seed(args):
    path = Path(args.from_agents_md)
    if not path.exists():
        print(f"File not found: {path}")
        sys.exit(1)

    content = path.read_text()
    lines = content.splitlines()
    data = _read_yaml()
    today = date.today().isoformat()
    project = args.project or _detect_project()
    count = 0

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("```") or stripped.startswith("---"):
            continue
        if len(stripped) < 10:
            continue

        cleaned = stripped.lstrip("-*•> ").strip()
        if len(cleaned) < 10:
            continue

        rid = _rule_id(cleaned)

        existing = None
        for r in data["rules"]:
            if r["id"] == rid:
                existing = r
                break

        if existing:
            already_from_this = any(
                o["project"] == project and "seeded from" in o.get("context", "")
                for o in existing["observations"]
            )
            if already_from_this:
                continue
            existing["observations"].append(
                {
                    "project": project,
                    "date": today,
                    "context": f"seeded from {path}",
                }
            )
            existing["total_count"] = len(existing["observations"])
            projects_seen = {o["project"] for o in existing["observations"]}
            existing["project_count"] = len(projects_seen)
            existing["cross_project"] = len(projects_seen) > 1
        else:
            data["rules"].append(
                {
                    "id": rid,
                    "rule": cleaned,
                    "domain": "unknown",
                    "created": today,
                    "last_reinforced": today,
                    "observations": [
                        {
                            "project": project,
                            "date": today,
                            "context": f"seeded from {path}",
                        }
                    ],
                    "cross_project": False,
                    "total_count": 1,
                    "project_count": 1,
                    "escalation": None,
                    "escalated_at": None,
                    "written_to": [{"path": str(path), "date": today}],
                }
            )
        count += 1

    _write_yaml(data)
    print(f"Seeded {count} rules from {path}")


def main():
    parser = argparse.ArgumentParser(
        prog="improve",
        description="Self-improving agent CLI — manages behavioral rules",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init", help="Initialize rules.yaml").add_argument(
        "--force", action="store_true"
    )

    obs = sub.add_parser("observe", help="Record a new observation")
    obs.add_argument("rule", help="The rule text to record")
    obs.add_argument("--project", "-p", help="Project name (auto-detected if omitted)")
    obs.add_argument("--domain", "-d", help="Domain category")
    obs.add_argument("--context", "-c", help="Context of the observation")
    obs.add_argument(
        "--explicit-scope", choices=["global", "local"], help="Override scope inference"
    )

    sub.add_parser("status", help="Show all rules and their status")

    sub.add_parser("due", help="Show rules pending escalation")

    esc = sub.add_parser("escalate", help="Mark rules for escalation")
    esc.add_argument("--dry-run", action="store_true", help="Show without writing")
    esc.add_argument("--apply", action="store_true", help="Write rules to AGENTS.md files")

    conf = sub.add_parser("confirm", help="Confirm a rule was written to AGENTS.md")
    conf.add_argument("rule_id", help="Rule ID to confirm")
    conf.add_argument("--scope", choices=["global", "local"], required=True)

    hist = sub.add_parser("history", help="Show observation history for a rule")
    hist.add_argument("rule_id", help="Rule ID to inspect")

    sub.add_parser("domains", help="List domain taxonomy")

    stale = sub.add_parser("stale", help="Show rules not reinforced in N days")
    stale.add_argument("--days", type=int, default=90, help="Staleness threshold in days")

    seed = sub.add_parser("seed", help="Import rules from an AGENTS.md file")
    seed.add_argument(
        "--from-agents-md", required=True, help="Path to AGENTS.md to seed from"
    )
    seed.add_argument("--project", "-p", help="Project name")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)

    commands = {
        "init": cmd_init,
        "observe": cmd_observe,
        "status": cmd_status,
        "due": cmd_due,
        "escalate": cmd_escalate,
        "confirm": cmd_confirm,
        "history": cmd_history,
        "domains": cmd_domains,
        "stale": cmd_stale,
        "seed": cmd_seed,
    }

    cmd_fn = commands.get(args.command)
    if cmd_fn:
        _ensure_init()
        cmd_fn(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
