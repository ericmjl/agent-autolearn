# /// script
# dependencies = ["pytest", "python-slugify"]
# ///
import json
import shift as S
import registry as R
from pathlib import Path


def persona(tmp_path: Path) -> Path:
    p = tmp_path / "default"
    p.mkdir()
    return p


def test_is_candidate_utterance():
    assert S.is_candidate_utterance("never use pip3 for this")
    assert S.is_candidate_utterance("always run tests before commit")
    assert not S.is_candidate_utterance("what time is it")


def test_record_sightings_idempotent(tmp_path):
    p = persona(tmp_path)
    n1 = S.record_sightings(["never use pip3"], "sess-a", "2026-06-01", p)
    n2 = S.record_sightings(["never use pip3"], "sess-a", "2026-06-01", p)
    assert n1 == 1 and n2 == 0


def test_sw_ema_handcheck(tmp_path):
    sightings = [
        {"topic_sig": "x", "date": "2026-06-01", "count": 1},
        {"topic_sig": "x", "date": "2026-06-05", "count": 2},
        {"topic_sig": "x", "date": "2026-06-10", "count": 3},
    ]
    sw, ema = S.compute_sw_ema(sightings)  # window=3 => mean(1,2,3)=2.0
    assert sw == 2.0
    # ema: seed=1; then 0.7*1+0.3*2=1.3; then 0.7*1.3+0.3*3=1.81
    assert abs(ema - 1.81) < 1e-6


def test_scan_creates_rising_candidate(tmp_path):
    # The literal "I keep saying this" case: same utterance across sessions
    # (steady recurrence, divergence≈0) must still fire via the recurrence floor.
    p = persona(tmp_path)
    S.record_sightings(["never commit secrets"], "s1", "2026-06-01", p)
    S.record_sightings(["never commit secrets"], "s2", "2026-06-10", p)
    S.record_sightings(["never commit secrets"], "s3", "2026-06-20", p)
    reg = R.MemoryRegistry(p)
    S.scan(reg, p, now="2026-06-25")
    cands = [c for c in S.load_candidates(p) if c["status"] == "pending"]
    assert len(cands) == 1
    assert cands[0]["direction"] == "rising"


def test_scan_auto_reinforces_existing_memory(tmp_path):
    p = persona(tmp_path)
    reg = R.MemoryRegistry(p)
    rec = reg.add("never commit secrets or api keys")  # topic tokens overlap the sightings
    before = len(reg.get(rec["id"])["reinforcements"])
    for sid, d in (("s1", "2026-06-01"), ("s2", "2026-06-10"), ("s3", "2026-06-20")):
        S.record_sightings(["never commit secrets"], sid, d, p)
    S.scan(reg, p, now="2026-06-25")
    after = len(reg.get(rec["id"])["reinforcements"])
    assert after > before  # actually reinforced, not a trivial 0>=0
