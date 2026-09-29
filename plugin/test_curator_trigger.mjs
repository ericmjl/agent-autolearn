#!/usr/bin/env node
/**
 * Tests for the activity-coupled curator trigger (issue #23).
 *
 * Run: node plugin/test_curator_trigger.mjs
 *
 * Sections:
 *   A. curatorDue() decision logic (injectable clock, config, file paths)
 *   B. maybeSpawnCurator() (stubbed spawn; cooldown + observation logging)
 *   C. Wrapper --curator mode E2E against a stubbed harness binary
 *      (single-flight gate, stale reclaim, spawned prompt, session cleanup)
 *   D. Review-path regression (min_interval_ms still gates reviews)
 *   E. Full-chain integration: runReviewSubprocess fires review + curator
 *   F. Large-review regression (issue #21: >32 KB review spawns via file path + stdin)
 *
 * All state lives under a temp AUTOLEARN_HOME; the real ~/.autolearn is
 * never touched. The module resolves its path constants from AUTOLEARN_HOME
 * at import time, so the env var is set before the dynamic import.
 */

import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync, rmSync, chmodSync } from "fs"
import { tmpdir } from "os"
import { join } from "path"
import { spawnSync } from "child_process"

const HOME = mkdtempSync(join(tmpdir(), "al-curator-test-"))
process.env.AUTOLEARN_HOME = HOME
process.env.AUTOLEARN_DEBUG = "1"

const { curatorDue, maybeSpawnCurator, ensureStore, runReviewSubprocess, OBS_FILE, WRAPPER_SCRIPT } = await import("./autolearn-core.mjs")

// win32 cannot exec the POSIX wrapper directly (EFTYPE); route it through
// Git Bash, mirroring the plugin's own spawn routing (ccfb8c6).
function findGitBash() {
  const candidates = [
    process.env.OPENCODE_GIT_BASH_PATH,
    "C:/Program Files/Git/bin/bash.exe",
    "C:/Program Files (x86)/Git/bin/bash.exe",
  ].filter(Boolean)
  for (const c of candidates) {
    try { if (existsSync(c)) return c } catch {}
  }
  return "bash"
}
function spawnWrapper(args, opts) {
  return process.platform === "win32"
    ? spawnSync(findGitBash(), [WRAPPER_SCRIPT, ...args], opts)
    : spawnSync(WRAPPER_SCRIPT, args, opts)
}

// Create the store (persona dirs, default config, wrapper) up front — the
// shells call this at startup, and logObs needs the persona dir to exist.
ensureStore()

let passed = 0
let failed = 0
function ok(cond, name) {
  if (cond) { passed++; console.log(`  ok - ${name}`) }
  else { failed++; console.error(`  FAIL - ${name}`) }
}
const DAY = 86400000

// ---------------------------------------------------------------------------
console.log("A. curatorDue()")

const stateFile = join(HOME, "state.json")
const cooldownFile = join(HOME, "cooldown")
const NOW = Date.UTC(2026, 8, 27, 12, 0, 0) // 2026-09-27T12:00Z

// Never run (missing state file) -> due
ok(curatorDue({ now: NOW, stateFile, cooldownFile, config: {} }).due === true, "never run -> due")

// Last run 8 days ago, interval 7 -> due; 6 days ago -> not due
writeFileSync(stateFile, JSON.stringify({ last_run: "2026-09-19" }))
ok(curatorDue({ now: NOW, stateFile, cooldownFile, config: {} }).due === true, "last run 8 days ago (interval 7) -> due")
writeFileSync(stateFile, JSON.stringify({ last_run: "2026-09-21" }))
ok(curatorDue({ now: NOW, stateFile, cooldownFile, config: {} }).due === false, "last run 6 days ago (interval 7) -> not due")
ok(curatorDue({ now: NOW, stateFile, cooldownFile, config: {} }).reason === "interval", "not-due reason is 'interval'")

// curators ran today -> not due (boundary: Date.parse of YYYY-MM-DD = UTC midnight)
writeFileSync(stateFile, JSON.stringify({ last_run: "2026-09-27" }))
ok(curatorDue({ now: NOW, stateFile, cooldownFile, config: {} }).due === false, "last run today -> not due")

// interval 0 disables, even when never run
ok(curatorDue({ now: NOW, stateFile, cooldownFile, config: { curator_interval_days: 0 } }).due === false, "interval 0 -> disabled")
ok(curatorDue({ now: NOW, stateFile, cooldownFile, config: { curator_interval_days: "0" } }).reason === "disabled", "interval '0' (string) -> disabled")

// interval from config is honored
writeFileSync(stateFile, JSON.stringify({ last_run: "2026-09-24" })) // 3 days ago
ok(curatorDue({ now: NOW, stateFile, cooldownFile, config: { curator_interval_days: 3 } }).due === true, "interval 3 with last run 3 days ago -> due")

// cooldown blocks a due run; an old cooldown does not
writeFileSync(stateFile, JSON.stringify({ last_run: "2026-09-01" }))
writeFileSync(cooldownFile, String(NOW - 60000)) // 1 min ago
const blocked = curatorDue({ now: NOW, stateFile, cooldownFile, config: {} })
ok(blocked.due === false && blocked.reason === "cooldown", "recent cooldown -> not due (reason 'cooldown')")
writeFileSync(cooldownFile, String(NOW - 2 * 3600000)) // 2h ago
ok(curatorDue({ now: NOW, stateFile, cooldownFile, config: {} }).due === true, "cooldown older than 1h -> due")

// malformed state file -> treated as never run
writeFileSync(stateFile, "not json{")
rmSync(cooldownFile, { force: true })
ok(curatorDue({ now: NOW, stateFile, cooldownFile, config: {} }).due === true, "malformed state file -> due (never-run semantics)")

// ---------------------------------------------------------------------------
console.log("B. maybeSpawnCurator()")

const obsBefore = existsSync(OBS_FILE) ? readFileSync(OBS_FILE, "utf-8") : ""

// Not due -> no spawn, no cooldown write
rmSync(cooldownFile, { force: true })
writeFileSync(stateFile, JSON.stringify({ last_run: "2026-09-27" }))
let spawned = []
const r1 = maybeSpawnCurator({ project: "p", now: NOW, stateFile, cooldownFile, spawnFn: (cmd) => spawned.push(cmd) })
ok(r1.ok === false && spawned.length === 0, "not due -> nothing spawned")

// Due -> wrapper spawned with --curator, cooldown written, observation logged
writeFileSync(stateFile, JSON.stringify({ last_run: "2026-09-01" })) // 26 days before NOW -> due, and observable
const r2 = maybeSpawnCurator({ project: "proj-x", cwd: "/tmp", now: NOW, stateFile, cooldownFile, spawnFn: (cmd, opts) => spawned.push([cmd, opts]) })
ok(r2.ok === true, "due -> spawns")
ok(spawned.length === 1 && spawned[0][0][0].endsWith("review-runner.sh") && spawned[0][0][1] === "--curator", "spawns wrapper with --curator")
ok(spawned[0][0][0] === WRAPPER_SCRIPT, "spawns the pinned WRAPPER_SCRIPT path")
ok(spawned[0][1].env.AUTOLEARN_CURATOR === "1", "spawn env carries AUTOLEARN_CURATOR=1")
ok(spawned[0][1].cwd === "/tmp", "spawn cwd honored")
ok(readFileSync(cooldownFile, "utf-8").trim() === String(NOW), "cooldown written at spawn time")
const obs = existsSync(OBS_FILE) ? readFileSync(OBS_FILE, "utf-8") : ""
const newObs = obs.slice(obsBefore.length)
ok(newObs.includes('"type":"curator_triggered"') && newObs.includes('"project":"proj-x"'), "curator_triggered observation logged")
ok(newObs.includes('"last_run":"2026-09-01"'), "observation carries last_run when known")

// spawnFn throwing must not propagate (never break the review path)
const r3 = maybeSpawnCurator({ project: "p", now: NOW + 7200000, stateFile, cooldownFile: join(HOME, "cd2"), spawnFn: () => { throw new Error("boom") } })
ok(r3.ok === false && r3.reason === "error", "spawn failure contained (ok:false, reason:error)")

// ---------------------------------------------------------------------------
console.log("C. Wrapper --curator mode (E2E vs stubbed harness binary)")

ok(existsSync(WRAPPER_SCRIPT), "ensureStore installed the wrapper")

const FAKE_LOG = join(HOME, "fake-oc.log")
const fakeOc = join(HOME, "fake-oc")
writeFileSync(fakeOc, `#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_OC_LOG"
if [ "$1" = "run" ]; then
  # The wrapper streams the review file on stdin (argv stays small, issue
  # #21); log it so content flow is observable.
  cat >> "$FAKE_OC_LOG" 2>/dev/null
  echo '{"sessionID": "fake-session-1"}'
fi
exit 0
`)
chmodSync(fakeOc, 0o755)

const env = {
  ...process.env,
  AUTOLEARN_HOME: HOME,
  AUTOLEARN_HARNESS_BIN: fakeOc,
  FAKE_OC_LOG: FAKE_LOG,
  AUTOLEARN_SYNC_API_KEY: "", // keep sync push silent
}
const fakeLines = () => (existsSync(FAKE_LOG) ? readFileSync(FAKE_LOG, "utf-8").split("\n").filter(Boolean) : [])

// Fresh run: gate acquired, curator agent invoked, session cleaned up, gate released
let r = spawnWrapper(["--curator"], { env, encoding: "utf-8", timeout: 15000 })
ok(r.status === 0, "wrapper --curator exits 0")
let lines = fakeLines()
ok(lines.length === 2, "stub harness called twice (run + session delete)")
ok(lines[0] && lines[0].includes("run --format json --agent autolearn-reviewer Load the autolearn skill and follow references/curator.md to run the curator."), "curator prompt + agent passed to the harness")
ok(lines[1] === "session delete fake-session-1", "curator session deleted after run (v1 branch)")
ok(!existsSync(join(HOME, ".curator_gate")), "curator gate released after run")

// Single-flight: a held gate skips the run
mkdirSync(join(HOME, ".curator_gate"), { recursive: true })
writeFileSync(join(HOME, ".curator_gate", "ts"), String(Math.floor(Date.now() / 1000)))
const before = fakeLines().length
r = spawnWrapper(["--curator"], { env, encoding: "utf-8", timeout: 15000 })
ok(r.status === 0 && fakeLines().length === before, "held gate -> second curator skipped (single-flight)")

// Stale gate (>1h) is reclaimed
writeFileSync(join(HOME, ".curator_gate", "ts"), String(Math.floor(Date.now() / 1000) - 3700))
r = spawnWrapper(["--curator"], { env, encoding: "utf-8", timeout: 15000 })
ok(r.status === 0 && fakeLines().length === before + 2, "stale gate (>1h) reclaimed -> curator runs")

// ---------------------------------------------------------------------------
console.log("D. Review-path regression (curator branch leaves review gates intact)")

// Reviews are passed as FILE PATHS; content streams on stdin (issue #21).
// First review runs; an immediate second review is blocked by min_interval_ms
const reviewFileD1 = join(HOME, "review-d1.md")
const reviewFileD2 = join(HOME, "review-d2.md")
writeFileSync(reviewFileD1, "Review content D1\n")
writeFileSync(reviewFileD2, "Review content D2 different\n")
r = spawnWrapper([reviewFileD1, "--agent", "autolearn-reviewer", "--title", "t"], { env, encoding: "utf-8", timeout: 15000 })
ok(r.status === 0, "first review exits 0")
const afterFirstReview = fakeLines().length
ok(afterFirstReview > before + 2, "first review invoked the harness")
r = spawnWrapper([reviewFileD2, "--agent", "autolearn-reviewer", "--title", "t"], { env, encoding: "utf-8", timeout: 15000 })
ok(r.status === 0 && fakeLines().length === afterFirstReview, "second immediate review blocked by min_interval_ms (gate intact)")

// ---------------------------------------------------------------------------
console.log("E. Full chain: runReviewSubprocess -> review spawn + curator trigger")

// Open the review interval gate (test speed only) and clear curator state so
// the trigger fires. Curator cooldown was written in section B under a test-
// local path; the real AL_HOME cooldown is still absent -> due.
const cfg = join(HOME, "personas", "default", "config.yaml")
writeFileSync(cfg, readFileSync(cfg, "utf-8").replace(/min_interval_ms: \d+/, "min_interval_ms: 0"))
rmSync(join(HOME, "personas", "default", ".curator_state.json"), { force: true })

// Detached wrappers land on the stub binary. The review md contains
// newlines, so line-count math is unreliable — poll for the SEMANTIC
// markers instead: the review content, the curator prompt, and both
// session-delete aftercare calls.
const reviewBefore = fakeLines().length
const obsBeforeE = readFileSync(OBS_FILE, "utf-8")
const res = runReviewSubprocess({
  reviewMd: "# Autolearn Review\n\n## Conversation\n\nINTEGRATION-MARKER-E\n",
  title: "integration",
  project: "integration-proj",
  trigger: "test",
  env,
})
ok(res.ok === true, "runReviewSubprocess accepted the spawn")

const has = (needle) => fakeLines().slice(reviewBefore).some((l) => l.includes(needle))
let deadline = Date.now() + 10000
while (
  Date.now() < deadline &&
  !(has("INTEGRATION-MARKER-E") && has("references/curator.md") &&
    fakeLines().slice(reviewBefore).filter((l) => l === "session delete fake-session-1").length >= 2)
) {
  await new Promise((res2) => setTimeout(res2, 200))
}
lines = fakeLines()
ok(has("INTEGRATION-MARKER-E"), "review content flowed through the wrapper")
ok(has("references/curator.md"), "curator prompt flowed through the wrapper")
ok(fakeLines().slice(reviewBefore).filter((l) => l === "session delete fake-session-1").length >= 2, "detached sessions cleaned up (review + curator)")
const obsE = readFileSync(OBS_FILE, "utf-8").slice(obsBeforeE.length)
ok(obsE.includes('"type":"curator_triggered"') && obsE.includes('"project":"integration-proj"'), "curator_triggered observed on the real path")

// ---------------------------------------------------------------------------
console.log("F. Large-review regression (issue #21: argv command-line limit)")

// A review larger than the Windows command-line limit (32,767 chars) must
// still spawn: runReviewSubprocess passes the review FILE PATH and the
// wrapper streams the file to the harness on stdin. Before the fix, the
// content rode in argv and the Windows spawn threw ENAMETOOLONG.
const bigMarker = "BIG-REVIEW-MARKER-F"
const bigReview = "# Autolearn Review\n\n## Conversation\n\n" + "x".repeat(40 * 1024) + "\n" + bigMarker + "\n"
const reviewBeforeF = fakeLines().length
const resF = runReviewSubprocess({ reviewMd: bigReview, title: "big", project: "big-proj", trigger: "test", env })
ok(resF.ok === true, "40 KB review spawn accepted (no argv limit)")
deadline = Date.now() + 10000
while (Date.now() < deadline && !fakeLines().slice(reviewBeforeF).some((l) => l.includes(bigMarker))) {
  await new Promise((res2) => setTimeout(res2, 200))
}
ok(fakeLines().slice(reviewBeforeF).some((l) => l.includes(bigMarker)), "40 KB review content flowed to the harness on stdin")

// ---------------------------------------------------------------------------

console.log(`\n${passed} passed, ${failed} failed`)
rmSync(HOME, { recursive: true, force: true })
process.exit(failed ? 1 : 0)
