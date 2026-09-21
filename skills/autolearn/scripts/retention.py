"""Retention engine — Ebbinghaus decay scoring, tiering, and grace-period eviction.

Embedding-free. Operates on registry records (see registry.py). The retention
score decays with age and is boosted by reinforcements; a record is evicted only
after remaining below the cold tier for the grace period.

Spec: docs/designs/memory-insight/LLD.md, retention-EARS.md (MI-RTN-*).
"""
from __future__ import annotations

import math
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import registry
from registry import MemoryRegistry

DEFAULT_CONFIG = {
    "retention_lambda": 0.03,
    "retention_sigma": 0.3,
    "retention_salience_memory": 0.85,
    "retention_salience_user": 0.80,
    "tier_hot": 0.7,
    "tier_warm": 0.4,
    "tier_cold": 0.15,
    "eviction_grace_days": 90,
}


def today() -> str:
    return date.today().isoformat()


def parse_date(s: str) -> date:
    return datetime.fromisoformat(s).date()


def days_between(a_iso: str, b_iso: str) -> int:
    try:
        return (parse_date(b_iso) - parse_date(a_iso)).days
    except (ValueError, TypeError):
        return 0


# @spec MI-RTN-002
def compute_salience(record: dict, config: dict) -> float:
    base = (
        config["retention_salience_memory"]
        if record.get("type") == "memory"
        else config["retention_salience_user"]
    )
    n = len(record.get("reinforcements") or [])
    return min(1.0, base + min(0.2, n * 0.02))


# @spec MI-RTN-001, MI-RTN-003
def compute_score(record: dict, config: dict = DEFAULT_CONFIG, now: str | None = None) -> float:
    now = now or today()
    created = record.get("created_at") or now
    days_since_created = max(0, days_between(created, now))
    temporal = math.exp(-config["retention_lambda"] * days_since_created)

    boost = 0.0
    for when in record.get("reinforcements") or []:
        d = days_between(when, now)
        if d > 0:  # MI-RTN-003: skip 0-day to avoid div-by-zero
            boost += 1.0 / d
    boost *= config["retention_sigma"]

    salience = compute_salience(record, config)
    return min(1.0, salience * temporal + boost)


# @spec MI-RTN-004
def tier_of(score: float, config: dict = DEFAULT_CONFIG) -> str:
    if score >= config["tier_hot"]:
        return "hot"
    if score >= config["tier_warm"]:
        return "warm"
    if score >= config["tier_cold"]:
        return "cold"
    return "evictable"


# @spec MI-RTN-005
def score_all(registry_obj: MemoryRegistry, config: dict = DEFAULT_CONFIG,
              now: str | None = None) -> dict:
    now = now or today()
    summary = {"total": 0, "hot": 0, "warm": 0, "cold": 0, "evictable": 0}
    for rec in registry_obj.load_active():
        score = compute_score(rec, config, now)
        tier = tier_of(score, config)
        rec["retention_score"] = round(score, 4)
        rec["tier"] = tier
        rec["scored_at"] = now
        registry_obj.update(rec)
        summary["total"] += 1
        summary[tier] += 1
    return summary


# @spec MI-RTN-006
def eviction_candidates(registry_obj: MemoryRegistry, config: dict = DEFAULT_CONFIG,
                        now: str | None = None) -> list[dict]:
    now = now or today()
    out = []
    for rec in registry_obj.load_active():
        score = rec.get("retention_score")
        if score is None:
            score = compute_score(rec, config, now)
        if tier_of(score, config) != "evictable":
            continue
        last = rec.get("last_reinforced") or rec.get("created_at") or now
        if days_between(last, now) >= config["eviction_grace_days"]:
            out.append(rec)
    return out


# @spec MI-RTN-007
def evict(registry_obj: MemoryRegistry, config: dict = DEFAULT_CONFIG,
          now: str | None = None, dry_run: bool = False) -> dict:
    now = now or today()
    cands = eviction_candidates(registry_obj, config, now)
    if not dry_run:
        for rec in cands:
            registry_obj.set_status(rec["id"], "evicted", now)
    return {
        "dry_run": dry_run,
        "evicted": 0 if dry_run else len(cands),
        "candidates": [{"id": c["id"], "score": c.get("retention_score")} for c in cands],
    }


# @spec MI-RTN-009
def curve_points(record: dict, config: dict = DEFAULT_CONFIG, days: int = 90) -> list[tuple[str, float]]:
    """Retention-over-time samples from created_at forward, modelling reinforcement bumps."""
    created = record.get("created_at") or today()
    try:
        start = parse_date(created)
    except (ValueError, TypeError):
        start = parse_date(today())
    reinforcements = sorted(record.get("reinforcements") or [])
    samples: list[tuple[str, float]] = []
    n = max(1, min(days, 365))
    step = max(1, n // 30)
    for i in range(0, n + 1, step):
        asof = (start + timedelta(days=i)).isoformat()
        if i == 0 and not reinforcements:
            # at creation, before any reinforcement: base salience*temporal(=1)
            score = min(1.0, compute_salience(record, config) * 1.0)
        else:
            effective = dict(record)
            effective["reinforcements"] = [r for r in reinforcements if days_between(r, asof) >= 0 and r <= asof]
            score = compute_score(effective, config, now=asof)
        samples.append((asof, round(score, 4)))
        if len(samples) >= 30:
            break
    if not samples:
        samples.append((start.isoformat(), compute_score(record, config, now=created)))
    return samples


def registry_for(args) -> MemoryRegistry:
    home = Path(os.environ.get("AUTOLEARN_HOME", Path.home() / ".autolearn"))
    persona = getattr(args, "persona", None) or "default"
    return MemoryRegistry(home / "personas" / persona)


# @spec MI-RTN-005
def cmd_retention_score(args):
    reg = registry_for(args)
    summary = score_all(reg)
    print(f"Scored {summary['total']} active memories:")
    for tier in ("hot", "warm", "cold", "evictable"):
        print(f"  {tier}: {summary[tier]}")


# @spec MI-RTN-007
def cmd_retention_evict(args):
    reg = registry_for(args)
    result = evict(reg, dry_run=bool(getattr(args, "dry_run", False)))
    label = "would evict" if result["dry_run"] else "evicted"
    print(f"{label} {result['evicted']} memories:")
    for c in result["candidates"]:
        print(f"  {c['id']} (score {c['score']})")
