# /// script
# dependencies = ["pytest", "python-slugify"]
# ///
import composer as C
import registry as R
from pathlib import Path


def persona(tmp_path: Path) -> Path:
    p = tmp_path / "default"
    p.mkdir()
    return p


def test_relevance_jaccard():
    rec = {"topics": ["python", "uv"], "text": "use uv for python"}
    assert C.relevance(rec, {"python", "uv"}) > 0.0
    assert C.relevance(rec, set()) == 0.0
    assert C.relevance(rec, {"rust", "cargo"}) == 0.0


def test_compose_empty_minimal(tmp_path):
    reg = R.MemoryRegistry(persona(tmp_path))
    md = C.compose(reg, set())
    assert "# Autolearn Memory" in md
    assert md.count("\n- ") == 0


def test_pinned_always_included_over_budget(tmp_path):
    reg = R.MemoryRegistry(persona(tmp_path))
    a = reg.add("pinned long entry " * 10, pinned=True)
    b = reg.add("also pinned " * 10, pinned=True)
    for r in (a, b):
        r["pinned"] = True
        reg.update(r)
    md = C.compose(reg, set(), config={"context_budget_chars": 50})
    assert "pinned long entry" in md and "also pinned" in md


def test_ranking_by_relevance_times_retention(tmp_path):
    reg = R.MemoryRegistry(persona(tmp_path))
    rel = reg.add("use uv for python tooling")
    irrel = reg.add("always format json with two spaces")
    for r, score in ((rel, 0.9), (irrel, 0.9)):
        r["retention_score"] = score
        reg.update(r)
    md = C.compose(reg, C.tokens("python uv"), config={"context_budget_chars": 3000})
    assert md.index("use uv for python") < md.index("always format json")


def test_no_context_falls_back_to_retention(tmp_path):
    reg = R.MemoryRegistry(persona(tmp_path))
    hot = reg.add("hot memory")
    cold = reg.add("cold memory")
    hot["retention_score"] = 0.95
    cold["retention_score"] = 0.1
    reg.update(hot)
    reg.update(cold)
    md = C.compose(reg, set())
    assert md.index("hot memory") < md.index("cold memory")


def test_format_header_exact(tmp_path):
    reg = R.MemoryRegistry(persona(tmp_path))
    reg.add("an entry")
    md = C.compose(reg, set())
    assert md.startswith("# Autolearn Memory\n\n<!-- Managed by autolearn. Do not edit the structure. -->\n\n")
