"""Memory registry — durable, unbounded store for autolearn memories.

Replaces the static, character-capped ``memory.md`` with a JSONL registry where
each record carries its own reinforcement history and (computed elsewhere)
Ebbinghaus retention state. This module is the foundation imported by
``retention.py``, ``composer.py``, ``shift.py`` and ``inspector_server.py``.

It is deliberately self-contained (no imports from ``autolearn.py``) to avoid
circular imports; the legacy markdown parsing logic is reproduced locally for
one-time migration.

Spec: docs/designs/memory-insight/LLD.md, registry-EARS.md (MI-REG-*).
"""
from __future__ import annotations

import json
import os
import re
import hashlib
from datetime import date, datetime
from pathlib import Path

from slugify import slugify as python_slugify

# @spec MI-REG-001
REGISTRY_FILE = "memories.jsonl"
LEGACY_MARKER = ".registry_migrated"  # idempotency marker for migrate_from_legacy
ID_MAX_LEN = 60

# Minimal English stopword set — no external dependency for topic extraction.
STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "if", "then", "else", "when", "at",
    "by", "for", "with", "about", "against", "between", "into", "through",
    "during", "before", "after", "above", "below", "to", "from", "up", "down",
    "in", "out", "on", "off", "over", "under", "again", "further", "is", "are",
    "was", "were", "be", "been", "being", "have", "has", "had", "do", "does",
    "did", "will", "would", "should", "could", "can", "may", "might", "must",
    "of", "as", "it", "its", "this", "that", "these", "those", "i", "you",
    "he", "she", "we", "they", "me", "him", "her", "us", "them", "my", "your",
    "their", "our", "not", "no", "so", "than", "too", "very", "s", "t",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# @spec MI-REG-003
def slugify_text(text: str) -> str:
    """Stable id slug for a memory text (python-slugify, truncated)."""
    slug = python_slugify(text.lower().strip())
    slug = slug[:ID_MAX_LEN].rstrip("-")
    return slug or "memory"


def tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokens (>=2 chars)."""
    return [t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) >= 2]


# @spec MI-REG-004
def extract_topics(text: str) -> list[str]:
    """Topic tokens: lowercased, punctuation/stopword-stripped, de-duplicated, order-preserving."""
    out: list[str] = []
    seen: set[str] = set()
    for tok in tokenize(text):
        if tok in STOPWORDS:
            continue
        if tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
    return out


def topic_signature(text: str) -> tuple[str, list[str]]:
    """Stable signature for shift-detector clustering: (sha1 of sorted topic tokens, tokens).

    Lexical, embedding-free. Two utterances cluster together when their
    topic-token sets are equal.
    """
    topics = sorted(extract_topics(text))
    sig = hashlib.sha1("|".join(topics).encode("utf-8")).hexdigest()[:16]
    return sig, topics


def extract_md_entries(md: str) -> list[str]:
    """Reproduced legacy markdown-entry parser (self-contained; no autolearn import).

    Skips HTML comments and headings, strips ``- `` / ``* `` list prefixes.
    """
    entries: list[str] = []
    in_comment = False
    for line in md.splitlines():
        stripped = line.strip()
        if stripped.startswith("<!--"):
            in_comment = True
        if in_comment:
            if "-->" in stripped:
                in_comment = False
            continue
        if stripped.startswith("#") or not stripped:
            continue
        if stripped.startswith("- ") or stripped.startswith("* "):
            entries.append(stripped[2:].strip())
        else:
            entries.append(stripped)
    return entries


def today() -> str:
    return date.today().isoformat()


def new_record(
    text: str,
    type_: str,
    *,
    pinned: bool = False,
    topics: list[str] | None = None,
    created_at: str | None = None,
    reinforcements: list[str] | None = None,
) -> dict:
    created = created_at or today()
    reinf = list(reinforcements or [])
    return {
        "id": slugify_text(text),
        "text": text.strip(),
        "type": type_,
        "created_at": created,
        "reinforcements": reinf,
        "last_reinforced": reinf[-1] if reinf else created,
        "pinned": bool(pinned),
        "topics": topics if topics is not None else extract_topics(text),
        "status": "active",
        "evicted_at": None,
        "retention_score": None,
        "tier": None,
        "scored_at": None,
    }


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class MemoryRegistry:
    """JSONL-backed memory registry for one persona directory.

    The persona dir is the same directory that holds ``memory.md`` /
    ``config.yaml`` (e.g. ``~/.autolearn/personas/default/``).
    """

    def __init__(self, persona_dir: Path):
        self.persona_dir = Path(persona_dir)
        self.path = self.persona_dir / REGISTRY_FILE
        self.persona_dir.mkdir(parents=True, exist_ok=True)

    # -- loading -----------------------------------------------------------
    # @spec MI-REG-005, MI-REG-017
    def load(self) -> list[dict]:
        """All records (active + evicted). Triggers lazy migration if absent."""
        if not self.path.exists():
            migrate_from_legacy(self.persona_dir)
        if not self.path.exists():
            return []
        records: list[dict] = []
        with self.path.open("r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    debug_log(self.persona_dir, f"skipping corrupt line {lineno}")
        return records

    def load_active(self) -> list[dict]:
        return [r for r in self.load() if r.get("status") == "active"]

    # -- lookup ------------------------------------------------------------
    def get(self, id: str) -> dict | None:
        for r in self.load():
            if r.get("id") == id:
                return r
        return None

    def find_by_text(self, text: str) -> dict | None:
        target = slugify_text(text)
        for r in self.load():
            if r.get("id") == target:
                return r
        return None

    # -- mutation ----------------------------------------------------------
    # @spec MI-REG-006
    def add(self, text: str, type: str = "memory", pinned: bool = False,
            topics: list[str] | None = None) -> dict:
        existing = self.find_by_text(text)
        if existing is not None:
            return existing
        rec = new_record(text, type, pinned=pinned, topics=topics)
        records = self.load()
        records.append(rec)
        self.save(records)
        return rec

    # @spec MI-REG-007
    def reinforce(self, id: str, when: str | None = None) -> dict | None:
        records = self.load()
        rec = next((r for r in records if r.get("id") == id), None)
        if rec is None:
            return None
        when = when or today()
        reinf = rec.setdefault("reinforcements", [])
        if not reinf or reinf[-1] != when:
            reinf.append(when)
        rec["last_reinforced"] = reinf[-1]
        if rec.get("status") == "evicted":  # a lesson came back
            rec["status"] = "active"
            rec["evicted_at"] = None
        self.save(records)
        return rec

    # @spec MI-REG-008
    def update(self, record: dict) -> None:
        records = self.load()
        for i, r in enumerate(records):
            if r.get("id") == record.get("id"):
                records[i] = record
                self.save(records)
                return
        # not found: treat as add
        records.append(record)
        self.save(records)

    def remove(self, id: str) -> bool:
        records = self.load()
        new = [r for r in records if r.get("id") != id]
        if len(new) == len(records):
            return False
        self.save(new)
        return True

    def set_pinned(self, id: str, value: bool) -> None:
        rec = self.get(id)
        if rec is not None:
            rec["pinned"] = bool(value)
            self.update(rec)

    def set_status(self, id: str, status: str, when: str | None = None) -> None:
        rec = self.get(id)
        if rec is not None:
            rec["status"] = status
            rec["evicted_at"] = when if status == "evicted" else None
            self.update(rec)

    # @spec MI-REG-009
    def save(self, records: list[dict]) -> None:
        # pid-unique tmp: concurrent autolearn processes (plugin-spawned
        # compose, scheduled jobs, installer) must not clobber each other's
        # tmp files during the write->replace window.
        tmp = self.path.with_suffix(self.path.suffix + f".{os.getpid()}.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp, self.path)


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

# @spec MI-REG-010, MI-REG-011, MI-REG-012, MI-REG-013, MI-REG-014
def migrate_from_legacy(persona_dir: Path) -> int:
    """One-time, idempotent migration from memory.md + user-profile.md + strengths.json.

    Returns the number of records written. No-op (returns 0) if already migrated
    or if the registry already exists.
    """
    persona_dir = Path(persona_dir)
    registry_path = persona_dir / REGISTRY_FILE
    marker = persona_dir / LEGACY_MARKER
    if registry_path.exists() or marker.exists():
        return 0

    memory_md = persona_dir / "memory.md"
    user_md = persona_dir / "user-profile.md"
    strengths_path = persona_dir / "strengths.json"

    strengths = {}
    if strengths_path.exists():
        try:
            strengths = json.loads(strengths_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            strengths = {}

    # Build a lookup from slug-prefix → strength record (handle truncation variance)
    strength_by_slug: dict[str, dict] = {}
    strength_prefixes: list[tuple[str, dict]] = []
    for slug, rec in strengths.items():
        strength_by_slug[slug] = rec
        if len(slug) >= 20:
            strength_prefixes.append((slug[:20], rec))

    def match_strength(text: str) -> dict | None:
        slug = slugify_text(text)
        if slug in strength_by_slug:
            return strength_by_slug[slug]
        # try full python-slugify of the lowercased text (legacy key format)
        legacy_key = python_slugify(text.lower().strip())[:ID_MAX_LEN].rstrip("-")
        if legacy_key in strength_by_slug:
            return strength_by_slug[legacy_key]
        for prefix, rec in strength_prefixes:  # @spec MI-REG-013 fallback
            if legacy_key.startswith(prefix):
                return rec
        return None

    records: list[dict] = []
    skips = 0
    mtime_fallback = None

    def resolve_mtime_fallback() -> str | None:
        nonlocal mtime_fallback
        if mtime_fallback is None and memory_md.exists():
            ts = memory_md.stat().st_mtime
            mtime_fallback = datetime.fromtimestamp(ts).date().isoformat()
        return mtime_fallback

    seen_ids: set[str] = set()
    for source_file, type_ in ((memory_md, "memory"), (user_md, "user")):
        if not source_file.exists():
            continue
        for entry in extract_md_entries(source_file.read_text(encoding="utf-8")):
            if not entry.strip():
                continue
            rec = new_record(entry, type_)
            if rec["id"] in seen_ids:
                continue
            s = match_strength(entry)
            if s is not None:
                first = s.get("first_seen") or resolve_mtime_fallback() or today()
                rec["created_at"] = first
                count = int(s.get("count", 1))
                last = s.get("last_seen") or first
                # synthesize reinforcement timestamps from count/last (best-effort)
                rec["reinforcements"] = [last] * max(0, count - 1)
                rec["last_reinforced"] = last or first
            else:
                rec["created_at"] = resolve_mtime_fallback() or today()
            seen_ids.add(rec["id"])
            records.append(rec)

    # @spec MI-REG-019: always materialize the registry file, even when zero
    # records were migrated. Without this, a fresh install (or a persona whose
    # legacy files contain only headers) leaves no memories.jsonl but still
    # writes the .registry_migrated marker — so migrate_from_legacy short-
    # circuits on every subsequent call and the file is never created. An empty
    # JSONL file is the valid representation of an empty registry.
    tmp = registry_path.with_suffix(registry_path.suffix + f".{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, registry_path)

    # @spec MI-REG-014: preserve legacy memory.md, stop loading it
    if memory_md.exists():
        legacy = persona_dir / "memory.md.legacy"
        try:
            if not legacy.exists():
                os.replace(memory_md, legacy)
        except OSError:
            pass  # leave original in place rather than risk data loss

    marker.write_text(today() + f"\n# migrated {len(records)} records, skipped {skips} orphans\n")
    return len(records)


def debug_log(persona_dir: Path, msg: str) -> None:
    """Best-effort debug log alongside the autolearn debug.log convention."""
    try:
        with (Path.home() / ".autolearn" / "debug.log").open("a", encoding="utf-8") as fh:
            fh.write(f"[registry] {msg}\n")
    except OSError:
        pass
