# /// script
# dependencies = ["pytest", "python-slugify"]
# ///
import math
import retention as RT
import registry as R
from pathlib import Path


def persona(tmp_path: Path) -> Path:
    p = tmp_path / "default"
    p.mkdir()
    return p


def make_rec(text="x", created="2026-06-01", reinforcements=None, type="memory"):
    return R.new_record(text, type, created_at=created, reinforcements=reinforcements or [])


def test_score_decreases_with_age():
    rec = make_rec(created="2026-06-01")
    young = RT.compute_score(rec, now="2026-06-02")
    old = RT.compute_score(rec, now="2026-07-02")
    assert young > old


def test_reinforcement_boosts_score():
    base = make_rec(created="2026-05-01", reinforcements=[])
    boosted = make_rec(created="2026-05-01", reinforcements=["2026-06-20"])
    b = RT.compute_score(base, now="2026-06-25")
    s = RT.compute_score(boosted, now="2026-06-25")
    assert s > b


def test_tier_boundaries():
    assert RT.tier_of(0.9) == "hot"
    assert RT.tier_of(0.5) == "warm"
    assert RT.tier_of(0.2) == "cold"
    assert RT.tier_of(0.05) == "evictable"


def test_score_all_writes_back(tmp_path):
    reg = R.MemoryRegistry(persona(tmp_path))
    reg.add("a memory", type="memory")
    summary = RT.score_all(reg, now="2026-06-25")
    assert summary["total"] == 1
    rec = reg.load_active()[0]
    assert rec["retention_score"] is not None
    assert rec["tier"] in {"hot", "warm", "cold", "evictable"}
    assert rec["scored_at"] == "2026-06-25"


def test_eviction_respects_grace(tmp_path):
    p = persona(tmp_path)
    reg = R.MemoryRegistry(p)
    rec = reg.add("ancient and forgotten")
    rec["created_at"] = "2024-01-01"
    rec["last_reinforced"] = "2024-01-01"
    rec["reinforcements"] = []
    reg.update(rec)
    RT.score_all(reg, now="2026-06-25")
    cands = RT.eviction_candidates(reg, now="2026-06-25")
    assert any(c["id"] == rec["id"] for c in cands)
    dry = RT.evict(reg, now="2026-06-25", dry_run=True)
    assert dry["evicted"] == 0  # dry run does not mutate
    assert reg.get(rec["id"])["status"] == "active"
    real = RT.evict(reg, now="2026-06-25", dry_run=False)
    assert real["evicted"] >= 1
    assert reg.get(rec["id"])["status"] == "evicted"


def test_curve_points_ascending_and_bounded():
    rec = make_rec(created="2026-05-01", reinforcements=["2026-05-15"])
    pts = RT.curve_points(rec, days=60)
    assert len(pts) <= 31 and len(pts) >= 1
    dates = [p[0] for p in pts]
    assert dates == sorted(dates)
