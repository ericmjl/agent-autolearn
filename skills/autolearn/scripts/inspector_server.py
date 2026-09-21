"""Inspector UI — a CLI-launchable local web app to explore the memory registry.

Stdlib-only HTTP server (no Flask/FastAPI/React). Serves a single embedded
HTML page (vanilla JS + inline SVG sparklines). Reads the live registry,
retention, shift, skills and observations data on every request.

Spec: docs/designs/memory-insight/LLD.md, inspector-ui-EARS.md (MI-UI-*).
"""
from __future__ import annotations

import json
import os
import socket
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import registry
from registry import MemoryRegistry

DEFAULT_PORT = 4321


def resolve_persona_dir(args=None) -> Path:
    home = Path(os.environ.get("AUTOLEARN_HOME", Path.home() / ".autolearn"))
    persona = (getattr(args, "persona", None) or "default") if args else "default"
    return home / "personas" / persona


# Defensive imports — these modules may be absent in stripped-down envs.
def curve(record: dict) -> list[list]:
    try:
        import retention  # type: ignore
        pts = retention.curve_points(record)
        return [[d, s] for d, s in pts]
    except Exception:
        return []


def rescore(record: dict) -> dict:
    try:
        import retention  # type: ignore
        record["retention_score"] = round(retention.compute_score(record), 4)
        record["tier"] = retention.tier_of(record["retention_score"])
        record["scored_at"] = registry.date.today().isoformat() if hasattr(registry, "date") else None
    except Exception:
        pass
    return record


def trend(record: dict) -> str:
    reinf = record.get("reinforcements") or []
    if len(reinf) < 2:
        return "steady"
    return "rising"


def load_candidates(pdir: Path, status: str | None) -> list[dict]:
    path = pdir / "candidates.jsonl"
    if not path.exists():
        return []
    out = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if status is None or row.get("status") == status:
                out.append(row)
    return out


def save_candidates(pdir: Path, candidates: list[dict]) -> None:
    path = pdir / "candidates.jsonl"
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for c in candidates:
            fh.write(json.dumps(c, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def load_skills(pdir: Path) -> dict:
    path = pdir / "skills" / ".usage.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def load_verdicts(pdir: Path) -> dict:
    """Per-skill falsification verdicts (Certified Procedures, Loop 1)."""
    path = pdir / "verdicts.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def load_shortcuts(pdir: Path) -> list:
    """Staged golden-path candidates (Certified Procedures, Loop 2)."""
    path = pdir / "shortcuts.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def load_activity(pdir: Path, n: int = 50) -> list[dict]:
    path = pdir / "observations.jsonl"
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows[-n:]


# ---------------------------------------------------------------------------
# Route dispatch — pure function, testable without a socket
# ---------------------------------------------------------------------------

def handle_request(method: str, path: str, body: bytes, persona_dir: Path):
    """Return (status, content_type, body_bytes)."""
    reg = MemoryRegistry(persona_dir)
    parsed = urlparse(path)
    route = parsed.path.rstrip("/") or "/"

    if method == "GET" and route == "/":
        return 200, "text/html; charset=utf-8", INDEX_HTML.encode("utf-8")

    if method == "GET" and route == "/api/overview":
        records = reg.load()
        tiers = {"hot": 0, "warm": 0, "cold": 0, "evictable": 0}
        unscored = 0
        for r in records:
            if r.get("status") == "evicted":
                continue
            t = r.get("tier")
            if t is None:
                unscored += 1
            elif t in tiers:
                tiers[t] += 1
        pending = load_candidates(persona_dir, "pending")
        learned = load_candidates(persona_dir, "learned")
        verdicts = load_verdicts(persona_dir)
        failing = sum(1 for v in verdicts.values() if v.get("verdict") == "fail")
        return json_response({
            "total": len([r for r in records if r.get("status") != "evicted"]),
            "tiers": tiers,
            "pending_candidates": len(pending),
            "learned": len(learned),
            "unscored": unscored,
            "failing_procedures": failing,
        })

    if method == "GET" and route == "/api/memories":
        rows = []
        for r in reg.load():
            if r.get("status") == "evicted":
                continue
            rows.append({
                "id": r.get("id"),
                "text": r.get("text"),
                "type": r.get("type"),
                "pinned": r.get("pinned", False),
                "retention_score": r.get("retention_score"),
                "tier": r.get("tier"),
                "strength": len(r.get("reinforcements") or []),
                "last_reinforced": r.get("last_reinforced"),
                "trend": trend(r),
                "status": r.get("status"),
            })
        return json_response(rows)

    if method == "GET" and route.startswith("/api/memory/"):
        mid = route.rsplit("/", 1)[-1]
        rec = reg.get(mid)
        if rec is None:
            return 404, "application/json", b'{"error":"not found"}'
        rec["curve"] = curve(rec)
        return json_response(rec)

    if method == "GET" and route == "/api/candidates":
        return json_response(load_candidates(persona_dir, "pending"))

    if method == "GET" and route == "/api/learned":
        return json_response(load_candidates(persona_dir, "learned"))

    if method == "GET" and route == "/api/skills":
        return json_response(load_skills(persona_dir))

    # @spec CP-INT-002 — Certified Procedures surfaces
    if method == "GET" and route == "/api/procedures":
        return json_response(load_verdicts(persona_dir))

    if method == "GET" and route == "/api/shortcuts":
        return json_response(load_shortcuts(persona_dir))

    # @spec WIKI-INS-001 — wiki layer surfaces
    if method == "GET" and route == "/api/wiki":
        import wiki as wiki_mod
        return json_response({
            "patterns": wiki_mod.list_patterns(persona_dir),
            "ledger": wiki_mod.read_ledger(persona_dir, last_n=20),
        })

    if method == "GET" and route == "/api/activity":
        return json_response(load_activity(persona_dir))

    if method == "POST" and route.startswith("/api/memory/") and route.endswith("/strengthen"):
        mid = route.split("/")[3]
        rec = reg.reinforce(mid)
        if rec is None:
            return 404, "application/json", b'{"error":"not found"}'
        rec = rescore(reg.get(mid) or rec)
        reg.update(rec)
        return json_response({"ok": True, "record": rec})

    if method == "POST" and route.startswith("/api/candidate/") and route.endswith("/confirm"):
        cid = route.split("/")[3]
        cands = load_candidates(persona_dir, None)
        target = next((c for c in cands if c.get("id") == cid), None)
        if target is None:
            return 404, "application/json", b'{"error":"not found"}'
        text = " ".join(target.get("tokens") or []) or (target.get("utterances") or [""])[0]
        reg.add(text, type="memory")
        target["status"] = "confirmed"
        save_candidates(persona_dir, cands)
        return json_response({"ok": True})

    if method == "POST" and route.startswith("/api/candidate/") and route.endswith("/dismiss"):
        cid = route.split("/")[3]
        cands = load_candidates(persona_dir, None)
        target = next((c for c in cands if c.get("id") == cid), None)
        if target is None:
            return 404, "application/json", b'{"error":"not found"}'
        target["status"] = "dismissed"
        save_candidates(persona_dir, cands)
        return json_response({"ok": True})

    return 404, "application/json", b'{"error":"no route"}'


def json_response(obj) -> tuple[int, str, bytes]:
    return 200, "application/json", json.dumps(obj, ensure_ascii=False).encode("utf-8")


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

def find_free_port(start: int, attempts: int = 10) -> int:
    for p in range(start, start + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return start  # last resort; let the HTTPServer error out clearly


def serve(port: int = DEFAULT_PORT, persona_dir: Path | None = None,
          open_browser: bool = True) -> None:
    persona_dir = persona_dir or resolve_persona_dir()
    chosen = find_free_port(port)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence default logging
            pass

        def dispatch(self, method: str):
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(length) if length else b""
            status, ctype, payload = handle_request(method, self.path, body, persona_dir)
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            self.dispatch("GET")

        def do_POST(self):
            self.dispatch("POST")

    url = f"http://127.0.0.1:{chosen}/"
    print(f"Inspector UI running at {url}")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    httpd = ThreadingHTTPServer(("127.0.0.1", chosen), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        httpd.server_close()


def cmd_ui(args):
    port = getattr(args, "port", DEFAULT_PORT) or DEFAULT_PORT
    open_browser = not bool(getattr(args, "no_browser", False))
    pdir = resolve_persona_dir(args)
    serve(port=port, persona_dir=pdir, open_browser=open_browser)


# ---------------------------------------------------------------------------
# Embedded single-page UI (vanilla JS + inline SVG)
# ---------------------------------------------------------------------------

INDEX_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Autolearn Inspector</title>
<style>
  body{background:#0d1117;color:#c9d1d9;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;margin:0;padding:0}
  header{background:#161b22;padding:14px 20px;border-bottom:1px solid #30363d;display:flex;gap:18px;align-items:center}
  header h1{font-size:15px;margin:0;font-weight:600}
  nav button{background:#21262d;color:#c9d1d9;border:1px solid #30363d;border-radius:6px;padding:6px 12px;cursor:pointer;font:inherit;font-size:13px}
  nav button.active{background:#1f6feb;border-color:#1f6feb;color:#fff}
  main{padding:20px;max-width:1100px;margin:0 auto}
  .cards{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:18px}
  .card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px 16px;min-width:110px}
  .card .n{font-size:22px;font-weight:700}
  .card .l{font-size:11px;color:#8b949e}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:left;padding:7px 8px;border-bottom:1px solid #21262d}
  th{color:#8b949e;font-weight:500;font-size:11px;text-transform:uppercase}
  tr:hover{background:#161b22;cursor:pointer}
  .badge{padding:2px 7px;border-radius:10px;font-size:11px}
  .hot{background:#238636;color:#fff}.warm{background:#d29922;color:#000}
  .cold{background:#6e7681;color:#fff}.evictable{background:#da3633;color:#fff}
  .pin{color:#f85149}
  .panel{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px;margin-top:14px}
  button.small{font-size:12px;padding:4px 9px}
  .mute{color:#8b949e}
</style></head><body>
<header><h1>Autolearn Inspector</h1><nav>
  <button data-v="overview" class="active">Overview</button>
  <button data-v="memories">Memories</button>
  <button data-v="candidates">Candidates</button>
  <button data-v="learned">Learned</button>
  <button data-v="skills">Skills</button>
  <button data-v="activity">Activity</button>
</nav></header>
<main><div id="view"></div></main>
<script>
const view=document.getElementById('view');
let cur='overview';
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{cur=b.dataset.v;
  document.querySelectorAll('nav button').forEach(x=>x.classList.toggle('active',x===b));render();});
async function api(p){const r=await fetch(p);return r.json();}
function tierBadge(t){return `<span class="badge ${t||'cold'}">${t||'-'}</span>`;}
async function render(){
  if(cur==='overview'){const o=await api('/api/overview');
    view.innerHTML=`<div class="cards">
      ${card(o.total,'active memories')}${card(o.tiers.hot,'hot')}${card(o.tiers.warm,'warm')}
      ${card(o.tiers.cold,'cold')}${card(o.tiers.evictable,'evictable')}${card(o.pending_candidates,'pending')}
      ${card(o.learned,'learned')}</div>
      <p class="mute">${o.unscored} unscored — run <code>autolearn retention score</code>.</p>`;
  } else if(cur==='memories'){const m=await api('/api/memories');
    view.innerHTML=`<table><thead><tr><th></th><th>memory</th><th>type</th><th>tier</th><th>score</th><th>strength</th><th>last</th></tr></thead><tbody>
      ${m.map(r=>`<tr onclick="detail('${r.id}')"><td>${r.pinned?'<span class=pin>\\u25c9</span>':''}</td>
      <td>${esc(r.text)}</td><td>${r.type}</td><td>${tierBadge(r.tier)}</td>
      <td>${r.retention_score==null?'-':r.retention_score}</td><td>${r.strength}</td><td>${r.last_reinforced||'-'}</td></tr>`).join('')}
      </tbody></table>`;
  } else if(cur==='candidates'){const c=await api('/api/candidates');
    view.innerHTML=c.length?c.map(x=>`<div class=panel><b>${esc(x.tokens.join(' '))}</b> ${tierBadge('warm')}
      <div class=mute>sw=${x.sw} ema=${x.ema} div=${x.divergence} dir=${x.direction}</div>
      ${(x.utterances||[]).map(u=>'<div class=mute>\\u201c'+esc(u)+'\\u201d</div>').join('')}
      <p><button class=small onclick="cact('${x.id}','confirm')">Remember it</button>
      <button class=small onclick="cact('${x.id}','dismiss')">Dismiss</button></p></div>`).join('')
      :'<p class=mute>No pending candidates.</p>';
  } else if(cur==='learned'){const l=await api('/api/learned');
    view.innerHTML=l.length?l.map(x=>`<div class=panel><b>${esc((x.tokens||[]).join(' '))}</b>
      <div class=mute>stopped recurring — the lesson took hold.</div></div>`).join('')
      :'<p class=mute>No falling topics yet.</p>';
  } else if(cur==='skills'){const s=await api('/api/skills');
    const rows=Object.entries(s);
    view.innerHTML=rows.length?`<table><thead><tr><th>skill</th><th>uses</th><th>patches</th><th>state</th></tr></thead><tbody>
      ${rows.map(([k,v])=>`<tr><td>${k}</td><td>${v.use_count||0}</td><td>${v.patch_count||0}</td><td>${v.state||'-'}</td></tr>`).join('')}
      </tbody></table>`:'<p class=mute>No skills.</p>';
  } else if(cur==='activity'){const a=await api('/api/activity');
    view.innerHTML=a.slice(-30).reverse().map(e=>`<div class=mute>${e.timestamp||''} · ${e.type||''}</div>`).join('')||'<p class=mute>No activity.</p>';
  }
}
function card(n,l){return `<div class=card><div class=n>${n}</div><div class=l>${l}</div></div>`;}
function esc(s){return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
async function detail(id){const r=await api('/api/memory/'+id);
  view.innerHTML=`<p><button class=small onclick="cur='memories';document.querySelectorAll('nav button')[1].click();">\\u2190 back</button></p>
    <div class=panel><b>${esc(r.text)}</b> ${tierBadge(r.tier)} ${r.pinned?'<span class=pin>\\u25c9 pinned</span>':''}
    <div class=mute>type ${r.type} · strength ${(r.reinforcements||[]).length} · created ${r.created_at} · last ${r.last_reinforced}</div>
    <p><button class=small onclick="mact('${id}','strengthen')">Strengthen</button></p>
    <h4>Retention curve</h4><div id="curve"></div></div>`;
  drawCurve(r.curve||[]);
}
async function mact(id,act){await fetch(`/api/memory/${id}/${act}`,{method:'POST'});detail(id);}
async function cact(id,act){await fetch(`/api/candidate/${id}/${act}`,{method:'POST'});render();}
function drawCurve(pts){const el=document.getElementById('curve');if(!pts||!pts.length){el.innerHTML='<p class=mute>no curve</p>';return;}
  const w=600,h=160,p=24;const xs=pts.map(p=>new Date(p[0]).getTime()),ys=pts.map(p=>p[1]);
  const xmin=Math.min(...xs),xmax=Math.max(...xs),ymin=0,ymax=1;
  const sx=x=>p+(x-xmin)/(xmax-xmin||1)*(w-2*p),sy=y=>h-p-(y-ymin)/(ymax-ymin||1)*(h-2*p);
  let d=pts.map(pt=>'L'+sx(new Date(pt[0]).getTime()).toFixed(1)+','+sy(pt[1]).toFixed(1)).join(' ').replace('L','M');
  el.innerHTML=`<svg width=${w} height=${h}><polyline fill=none stroke=#1f6feb stroke-width=2 points="${pts.map(pt=>sx(new Date(pt[0]).getTime()).toFixed(1)+','+sy(pt[1]).toFixed(1)).join(' ')}"/></svg>`;}
render();
</script></body></html>
"""
