"""Shift detector — catches recurring (and recanted) user preferences across sessions.

Per topic signature, tracks a short-term sliding window (SW) vs a long-term
exponential moving average (EMA). Rising divergence => candidate preference
(auto-reinforced if it matches an existing memory, else staged for UI
confirmation). Falling divergence => a successful learning ("learned").
Embedding-free: lexical topic signatures + Jaccard matching.

Spec: docs/designs/memory-insight/LLD.md, shift-detector-EARS.md (MI-SFT-*).
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import date
from pathlib import Path

import registry
from registry import MemoryRegistry, topic_signature

DEFAULT_CONFIG = {
    "shift_window": 3,
    "shift_beta": 0.7,
    "shift_divergence_threshold": 0.5,
    "shift_recurrence_floor": 1.0,
    "shift_min_sessions": 2,
    "shift_max_candidates": 20,
}

CUE_WORDS = {
    "don't", "dont", "never", "always", "should", "prefer", "again", "keep",
    "stop", "wrong", "instead", "rather", "use", "avoid",
}
LEAD_RE = __import__("re").compile(r"^\s*(use|don't|dont|never|always|stop|avoid)\b", __import__("re").IGNORECASE)

TOPICS_FILE = "topics.jsonl"
CANDIDATES_FILE = "candidates.jsonl"


def today() -> str:
    return date.today().isoformat()


# @spec MI-SFT-001
def is_candidate_utterance(text: str) -> bool:
    if not text:
        return False
    lower = text.lower()
    if LEAD_RE.match(text):
        return True
    toks = set(__import__("re").findall(r"[a-z']+", lower))
    return bool(toks & CUE_WORDS)


def topics_path(persona_dir: Path) -> Path:
    return Path(persona_dir) / TOPICS_FILE


def candidates_path(persona_dir: Path) -> Path:
    return Path(persona_dir) / CANDIDATES_FILE


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


# @spec MI-SFT-002, MI-SFT-003
def record_sightings(messages: list[str], session_id: str, when: str,
                     persona_dir: Path) -> int:
    path = topics_path(persona_dir)
    existing = load_jsonl(path)
    already = {(r["topic_sig"], r["session_id"]) for r in existing}

    counts: dict[str, dict] = {}
    order: list[str] = []
    for msg in messages:
        if not is_candidate_utterance(msg):
            continue
        sig, tokens = topic_signature(msg)
        if sig not in counts:
            counts[sig] = {"tokens": tokens, "count": 0, "sample": msg[:120]}
            order.append(sig)
        counts[sig]["count"] += 1

    appended = 0
    with path.open("a", encoding="utf-8") as fh:
        for sig in order:
            if (sig, session_id) in already:
                continue  # idempotent per session
            c = counts[sig]
            row = {
                "topic_sig": sig,
                "tokens": c["tokens"],
                "session_id": session_id,
                "date": when,
                "count": c["count"],
                "text_sample": c["sample"],
            }
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            appended += 1
    return appended


# @spec MI-SFT-004
def compute_sw_ema(sightings_for_sig: list[dict], config: dict = DEFAULT_CONFIG) -> tuple[float, float]:
    by_date: dict[str, int] = {}
    for s in sightings_for_sig:
        d = s.get("date")
        by_date[d] = by_date.get(d, 0) + int(s.get("count", 0))
    dates = sorted(by_date.keys())
    if not dates:
        return (0.0, 0.0)
    window = config["shift_window"]
    recent = dates[-window:]
    sw = sum(by_date[d] for d in recent) / len(recent) if recent else 0.0
    beta = config["shift_beta"]
    ema = float(by_date[dates[0]])
    for d in dates[1:]:
        ema = beta * ema + (1 - beta) * float(by_date[d])
    return (sw, ema)


def jaccard(a: list[str], b: list[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def load_candidates(persona_dir: Path) -> list[dict]:
    return load_jsonl(candidates_path(persona_dir))


def save_candidates(persona_dir: Path, candidates: list[dict]) -> None:
    path = candidates_path(persona_dir)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for c in candidates:
            fh.write(json.dumps(c, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


# @spec MI-SFT-004, MI-SFT-005, MI-SFT-006, MI-SFT-007, MI-SFT-008, MI-SFT-009
def scan(reg: MemoryRegistry, persona_dir: Path, config: dict = DEFAULT_CONFIG,
         now: str | None = None) -> list[dict]:
    now = now or today()
    sightings = load_jsonl(topics_path(persona_dir))
    by_sig: dict[str, list[dict]] = {}
    for s in sightings:
        by_sig.setdefault(s["topic_sig"], []).append(s)

    candidates = load_candidates(persona_dir)
    by_csig = {c["topic_sig"]: c for c in candidates}
    changed: list[dict] = []

    active = reg.load_active()

    for sig, rows in by_sig.items():
        sw, ema = compute_sw_ema(rows, config)
        divergence = abs(sw - ema)
        distinct_sessions = len({r.get("session_id") for r in rows})
        tokens = rows[-1].get("tokens") or []
        samples = [r.get("text_sample", "") for r in rows][-5:]
        sessions = sorted({r.get("session_id") for r in rows})
        existing = by_csig.get(sig)
        floor = config.get("shift_recurrence_floor", 1.0)
        active_now = sw >= floor

        # A topic "rises" if seen across enough sessions AND it is either
        # accelerating (divergence) or steadily recurring (floor). The floor
        # clause is what catches the literal "I keep saying this" case, which
        # pure divergence (steady ⇒ divergence≈0) would miss.
        rising = (
            distinct_sessions >= config["shift_min_sessions"]
            and (divergence >= config["shift_divergence_threshold"] or active_now)
        )

        if rising:  # @spec MI-SFT-005, MI-SFT-006
            direction = "rising"
            # MI-SFT-007: auto-reinforce a matching memory
            match = None
            for rec in active:
                if jaccard(rec.get("topics") or [], tokens) >= 0.5:
                    match = rec
                    break
            if match is not None:
                reg.reinforce(match["id"], now)
                if existing and existing.get("status") == "pending":
                    existing["status"] = "confirmed"
                    changed.append(existing)
                continue
            # MI-SFT-008: novel rising -> pending candidate
            if existing is None:
                cand = {
                    "id": uuid.uuid4().hex[:12],
                    "topic_sig": sig,
                    "tokens": tokens,
                    "sw": round(sw, 3),
                    "ema": round(ema, 3),
                    "divergence": round(divergence, 3),
                    "direction": "rising",
                    "first_seen": rows[0].get("date", now),
                    "sessions": sessions,
                    "utterances": samples,
                    "status": "pending",
                    "created_at": now,
                }
                candidates.append(cand)
                by_csig[sig] = cand
                changed.append(cand)
            else:
                existing.update(sw=round(sw, 3), ema=round(ema, 3),
                                divergence=round(divergence, 3), direction="rising",
                                sessions=sessions, utterances=samples)
                if existing.get("status") in ("dismissed", "learned"):
                    existing["status"] = "pending"
                changed.append(existing)
        elif existing is not None and not active_now and sw < ema:
            # @spec MI-SFT-009: falling -> learned (closed-loop success signal)
            if existing.get("status") not in ("confirmed", "dismissed"):
                existing["status"] = "learned"
                existing.update(sw=round(sw, 3), ema=round(ema, 3),
                                divergence=round(divergence, 3), direction="falling")
                changed.append(existing)

    # MI-SFT-008: cap pending at shift_max_candidates (drop lowest divergence)
    pending = [c for c in candidates if c.get("status") == "pending"]
    if len(pending) > config["shift_max_candidates"]:
        pending.sort(key=lambda c: c.get("divergence", 0))
        for c in pending[: len(pending) - config["shift_max_candidates"]]:
            c["status"] = "dismissed"

    save_candidates(persona_dir, candidates)
    return changed


def registry_for(args) -> tuple[MemoryRegistry, Path]:
    home = Path(os.environ.get("AUTOLEARN_HOME", Path.home() / ".autolearn"))
    persona = getattr(args, "persona", None) or "default"
    pdir = home / "personas" / persona
    return MemoryRegistry(pdir), pdir


# @spec MI-SFT-010
def cmd_topics_scan(args):
    reg, pdir = registry_for(args)
    changed = scan(reg, pdir)
    if not changed:
        print("No new/updated candidates.")
    for c in changed:
        print(f"  [{c.get('status')}] {c.get('direction')} div={c.get('divergence')} "
              f"tokens={' '.join(c.get('tokens', [])[:6])}")


# @spec MI-SFT-011
def cmd_topics_candidates(args):
    _reg, pdir = registry_for(args)
    pending = [c for c in load_candidates(pdir) if c.get("status") == "pending"]
    if not pending:
        print("No pending candidates.")
    for c in pending:
        print(f"  div={c.get('divergence')} sw={c.get('sw')} ema={c.get('ema')}")
        for u in c.get("utterances", [])[:2]:
            print(f"    \"{u}\"")
