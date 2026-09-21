"""Context composer — builds the per-session memory.context.md from the registry.

Ranks active memories by relevance x retention into a soft character budget.
Replaces the static, FIFO-capped memory.md view. Embedding-free (lexical
Jaccard relevance).

Spec: docs/designs/memory-insight/LLD.md, composer-EARS.md (MI-CMP-*).
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import registry
from registry import MemoryRegistry

DEFAULT_CONFIG = {"context_budget_chars": 3000}

HEADER = "# Autolearn Memory\n\n<!-- Managed by autolearn. Do not edit the structure. -->\n\n"


def tokens(text_or_list) -> set[str]:
    if isinstance(text_or_list, (list, tuple, set)):
        text = " ".join(str(t) for t in text_or_list)
    else:
        text = str(text_or_list)
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) >= 2}


# @spec MI-CMP-006
def relevance(record: dict, ctx_tokens: set[str]) -> float:
    if not ctx_tokens:
        return 0.0
    record_tokens = set(record.get("topics") or []) | tokens(record.get("text", ""))
    if not record_tokens:
        return 0.0
    inter = len(record_tokens & ctx_tokens)
    union = len(record_tokens | ctx_tokens)
    return inter / union if union else 0.0


def retention(record: dict) -> float:
    score = record.get("retention_score")
    return 0.5 if score is None else float(score)


# @spec MI-CMP-001, MI-CMP-002, MI-CMP-003, MI-CMP-004, MI-CMP-005, MI-CMP-007, MI-CMP-008
def compose(registry_obj: MemoryRegistry, ctx_tokens: set[str],
            config: dict = DEFAULT_CONFIG) -> str:
    records = registry_obj.load_active()
    if not records:
        return HEADER  # MI-CMP-008

    pinned = [r for r in records if r.get("pinned")]
    others = [r for r in records if not r.get("pinned")]

    has_ctx = bool(ctx_tokens)
    others.sort(
        key=lambda r: (relevance(r, ctx_tokens) * retention(r)) if has_ctx else retention(r),
        reverse=True,
    )

    budget = config["context_budget_chars"]
    header_len = len(HEADER)
    used = header_len
    chosen: list[dict] = list(pinned)  # MI-CMP-003: pinned first, always

    def entry_len(text: str) -> int:
        return len(f"- {text}\n")

    for rec in others:
        cost = entry_len(rec.get("text", ""))
        if used + cost > budget:
            continue  # MI-CMP-004/005: respect budget for non-pinned
        chosen.append(rec)
        used += cost

    out = HEADER
    for rec in chosen:  # pinned then ranked
        out += f"- {rec.get('text', '')}\n"
    return out


# @spec MI-CMP-001, MI-CMP-009
def cmd_compose(args):
    home = Path(os.environ.get("AUTOLEARN_HOME", Path.home() / ".autolearn"))
    persona = getattr(args, "persona", None) or "default"
    persona_dir = home / "personas" / persona
    reg = MemoryRegistry(persona_dir)

    ctx = getattr(args, "context", None)
    ctx_tokens = tokens(ctx) if ctx else set()

    md = compose(reg, ctx_tokens)
    out_path = persona_dir / "memory.context.md"
    tmp = out_path.with_suffix(out_path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(md, encoding="utf-8")
    os.replace(tmp, out_path)

    n_entries = max(0, md.count("\n- "))
    print(f"Wrote {out_path} ({n_entries} entries, {len(md)} chars)")
