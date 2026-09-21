/**
 * Autolearn Plugin — pi shell (https://github.com/earendil-works/pi-coding-agent).
 *
 * Same enforced methodology as the OpenCode shells (plugin/autolearn.js,
 * plugin/autolearn-v2.js), adapted to pi's ExtensionAPI. All shared logic
 * lives in ./autolearn-core.mjs; this file only contains pi-specific event
 * plumbing.
 *
 * Event mapping (opencode concept → pi event):
 *   session.created        → session_start (reason "startup"|"new"|"resume"|"fork")
 *   user message           → message_end { message.role === "user" }
 *   assistant completion   → message_end { message.role === "assistant" }
 *                             (exchange boundary; threshold checked here)
 *   session.idle           → agent_settled (run fully settled, cooldown-gated)
 *   process exit           → session_shutdown (exit review; also flushes the
 *                             buffer on session switches, which pi allows
 *                             mid-process unlike OpenCode)
 *   instructions file      → before_agent_start injects memory.context.md as
 *                             a system-prompt SECTION (pi diffs sections and
 *                             appends a patch message, so the provider cache
 *                             survives; no opencode.json needed)
 *
 * The threshold unit is USER messages (exchanges), default 5 — identical to
 * the OpenCode shells (see core.THRESHOLD_DEFAULT and docs/conversation-monitoring/).
 *
 * Deliberately imports NOTHING beyond Node builtins, the local core, and a
 * type-only import of pi's ExtensionAPI (stripped at load by jiti), so this
 * file resolves without node_modules.
 *
 * Environment variables (same as the other shells):
 *   AUTOLEARN_HOME     - Base directory (default: ~/.autolearn)
 *   AUTOLEARN_DISABLED - Set to "1" to disable
 *   AUTOLEARN_DEBUG    - Set to "1" for debug logging
 *   AUTOLEARN_REVIEWER - Set by core when spawning review subprocesses; the
 *                        shell skips monitoring entirely when present, so a
 *                        review never reviews itself.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent"
import { readFileSync, writeFileSync } from "node:fs"
import * as core from "./autolearn-core.mjs"

// Loosely-typed event payloads (pi's full types live behind the type-only
// ExtensionAPI import; the shapes we consume are documented in extensions.md).
type AnyEvent = any

interface BufferedMessage {
  role: "user" | "assistant"
  content: string
  timestamp: string
}

export default function autolearnPi(pi: ExtensionAPI) {
  if (process.env.AUTOLEARN_DISABLED === "1") return
  if (process.env.AUTOLEARN_REVIEWER === "1") {
    core.dbg("SKIPPING (pi): reviewer session, not counting turns")
    return
  }

  core.ensureStore()
  // `config` must be mutable — re-read at every trigger point so live edits
  // to config.yaml take effect without a reload (same as the v2 shell).
  let config = core.parseConfig()

  let buffer: BufferedMessage[] = []
  let userMsgCount = 0
  let lastReviewUserMsg = 0
  let lastIdleReview = 0
  let reviewInProgress = false
  // Fallback flag: only used when systemPromptOptions.sections is unavailable
  // (older pi); the one-shot systemPrompt append must not repeat every turn.
  let memoryAppended = false
  let cwd = process.cwd()

  const projectName = () => cwd.split("/").pop() || "unknown"

  // Extract plain text from a pi message `content` (string or content blocks).
  function textOf(content: any): string {
    if (typeof content === "string") return content
    if (Array.isArray(content)) {
      return content
        .filter((b: any) => b?.type === "text" && typeof b.text === "string")
        .map((b: any) => b.text)
        .join("\n\n")
    }
    return ""
  }

  function trimBuffer() {
    const maxBuf = config.max_conversation_buffer || 50
    if (buffer.length > maxBuf) buffer = buffer.slice(-maxBuf)
  }

  // @spec CM-RS-003, CM-RS-004, CM-RS-005 (pi: message_end boundary)
  async function spawnReview(trigger: string) {
    if (buffer.length === 0 || reviewInProgress) return
    const reviewText = buffer.map(m => m.content).join(" ")
    if (reviewText.includes(core.REVIEW_HEADING)) {
      core.dbg("SKIPPING (pi): buffer contains review content (depth guard)")
      buffer = []
      return
    }
    // Speculate on the review content BEFORE clearing the buffer: if the
    // throttle denies the spawn (busy window / duplicate), the buffer stays
    // intact and this content rides the NEXT trigger instead of being lost.
    // PEEK ONLY (commit: false) — runReviewSubprocess owns the committing
    // gate; a committing pre-check would self-cancel every review (issue #14).
    const reviewMd = core.formatReview(buffer, { project: projectName(), trigger })
    if (!core.throttleCheck(reviewMd, false)) {
      core.dbg("REVIEW QUEUED by throttle (pi)", buffer.length, "messages, trigger", trigger)
      return
    }

    reviewInProgress = true
    // @spec CM-BUF-003
    const captured = [...buffer]
    buffer = []

    core.dbg("SPAWN REVIEW (pi)", captured.length, "messages, trigger", trigger)

    try {
      core.runReviewSubprocess({
        reviewMd,
        title: "autolearn review",
        cwd,
        // Pin the wrapper to the pi binary: reviews run as one-shot
        // print-mode processes (--no-session, no cleanup needed).
        env: { AUTOLEARN_HARNESS_BIN: "pi" },
        messageCount: captured.length,
        project: projectName(),
        trigger,
      })
      // @spec CM-RS-014
      core.cleanStaleReviews(config)
    } catch (err: any) {
      // @spec CM-RS-011, CM-RS-012
      core.dbg("REVIEW SPAWN FAILED (pi)", err?.message)
      console.error("[autolearn] Review spawn failed:", err?.message)
      try {
        writeFileSync(join(core.AL_HOME, `review-failed-${Date.now()}.md`), reviewMd)
      } catch {}
    } finally {
      // @spec CM-RS-005
      reviewInProgress = false
    }
  }

  // @spec CM-RS-016..CM-RS-019 (pi: session_shutdown instead of process signals)
  function spawnExitReview() {
    if (buffer.length <= 2) return
    const reviewText = buffer.map(m => m.content).join(" ")
    if (reviewText.includes(core.REVIEW_HEADING)) return

    const captured = [...buffer]
    buffer = []

    try {
      core.runReviewSubprocess({
        reviewMd: core.formatReview(captured, { project: projectName(), trigger: "exit" }),
        filePrefix: "review-exit",
        title: "autolearn exit review",
        cwd,
        env: { AUTOLEARN_HARNESS_BIN: "pi" },
        log: false, // exit path stays silent, matching the other shells
      })
      core.dbg("EXIT REVIEW SPAWNED (pi)", captured.length, "messages")
    } catch (err: any) {
      core.dbg("EXIT REVIEW FAILED (pi)", err?.message)
    }
  }

  // ------------------------------------------------------------------
  // Lifecycle
  // ------------------------------------------------------------------

  pi.on("session_start", async (_event: AnyEvent, ctx: any) => {
    if (ctx?.cwd) cwd = ctx.cwd
    core.dbg("PLUGIN SESSION START (pi)", { cwd, mode: ctx?.mode })
    // @spec SYNC-PROTO-012
    core.syncBackground("pull")
  })

  // Memory Insight for pi: inject the composed memory context as a system
  // prompt section. Mutating `sections` (documented-preferred path) lets pi
  // diff against what the model already has — first turn appends one patch
  // message, later turns are no-ops unless a review rewrote memory.context.md.
  pi.on("before_agent_start", async (event: AnyEvent) => {
    try {
      let memory = ""
      try { memory = readFileSync(core.MEMORY_FILE, "utf-8") } catch {}
      if (!memory.trim()) return undefined
      const opts = event?.systemPromptOptions
      if (opts && opts.sections && typeof opts.sections === "object") {
        opts.sections["autolearn-memory"] = memory
        return undefined
      }
      if (!memoryAppended) {
        memoryAppended = true
        const base = typeof event?.systemPrompt === "string" ? event.systemPrompt : ""
        return { systemPrompt: base + "\n\n# Autolearn Memory\n\n" + memory }
      }
    } catch (err: any) {
      core.dbg("MEMORY INJECT FAILED (pi)", err?.message)
    }
    return undefined
  })

  // @spec CM-TC-003..CM-TC-007 (pi: full messages arrive at message_end)
  pi.on("message_end", async (event: AnyEvent) => {
    try {
      const message = event?.message
      const role = message?.role
      if (role !== "user" && role !== "assistant") return

      const raw = textOf(message.content)
      if (!raw.trim()) return // tool-only assistant messages, empty/custom messages

      if (role === "user") {
        // @spec CM-TC-004, CM-TC-007 (threshold unit = USER messages)
        const content = core.redact(core.truncate(raw, 1000))
        buffer.push({ role: "user", content, timestamp: new Date().toISOString() })
        trimBuffer()
        userMsgCount++
        core.dbg("USER MESSAGE (pi)", content.length, "chars, total", userMsgCount)
      } else {
        // @spec CM-TC-005, CM-TC-001
        const content = core.redact(core.truncate(raw, 2000))
        buffer.push({ role: "assistant", content, timestamp: new Date().toISOString() })
        trimBuffer()
        core.dbg("ASSISTANT TURN (pi)", content.length, "chars")

        // @spec CM-RS-001, CM-RS-002 (threshold counts USER messages; the
        // assistant-completion boundary means reviews always cover complete
        // exchanges). Re-read config at trigger time so live edits to
        // config.yaml take effect without a reload.
        config = core.parseConfig()
        const threshold = config.review_threshold || core.THRESHOLD_DEFAULT
        if (userMsgCount - lastReviewUserMsg >= threshold) {
          lastReviewUserMsg = userMsgCount
          core.dbg("TRIGGERING REVIEW (pi) after user msg", userMsgCount)
          spawnReview("threshold").catch((e: any) => {
            core.dbg("SPAWN REVIEW UNHANDLED (pi)", e?.message)
            reviewInProgress = false
          })
        }
      }
    } catch (err: any) {
      core.dbg("MESSAGE_END ERROR (pi)", err?.message)
    }
    return undefined
  })

  // @spec CM-IDLE-001..CM-IDLE-004 (pi: agent_settled = the run is fully
  // done — no retry/compaction/queued follow-up will happen automatically).
  pi.on("agent_settled", async () => {
    try {
      const now = Date.now()
      config = core.parseConfig()
      const cooldown = config.idle_cooldown_ms || core.IDLE_COOLDOWN_MS
      if (
        config.session_review_on_idle !== false &&
        buffer.length > 2 &&
        !reviewInProgress &&
        now - lastIdleReview >= cooldown
      ) {
        lastIdleReview = now
        core.dbg("IDLE REVIEW (pi) buffer=", buffer.length)
        spawnReview("idle").catch((e: any) => {
          core.dbg("IDLE REVIEW UNHANDLED (pi)", e?.message)
          reviewInProgress = false
        })
      }
    } catch (err: any) {
      core.dbg("AGENT_SETTLED ERROR (pi)", err?.message)
    }
  })

  // @spec CM-RS-016..CM-RS-019 — pi's session teardown is the exit hook.
  // reason "quit": flush the tail as an exit review. reason
  // "new"|"resume"|"fork": the conversation is ending too (pi switches
  // sessions mid-process, unlike OpenCode) — flush the same way. The
  // throttle gates (dedupe, min_interval, daily cap) make both safe.
  pi.on("session_shutdown", async (event: AnyEvent) => {
    core.dbg("SESSION SHUTDOWN (pi)", event?.reason)
    spawnExitReview()
  })

  // Manual trigger: /autolearn-review forces a review of the current buffer
  // (still throttle-gated). Handy for testing and for "learn from this now".
  pi.registerCommand("autolearn-review", {
    description: "Spawn an autolearn review of the current conversation buffer",
    handler: async () => {
      config = core.parseConfig()
      await spawnReview("manual")
    },
  })
}
