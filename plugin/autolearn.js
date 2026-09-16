/**
 * Autolearn Plugin — OpenCode v1 shell.
 *
 * Counts conversation turns and spawns a review subagent at thresholds.
 * All shared logic lives in ./autolearn-core.mjs; this file only contains
 * the v1 plugin API surface (function default export + v1 event shapes).
 *
 * For OpenCode v2 (beta), use ./autolearn-v2.js instead.
 *
 * Environment variables:
 *   AUTOLEARN_HOME     - Base directory (default: ~/.autolearn)
 *   AUTOLEARN_DISABLED - Set to "1" to disable
 *   AUTOLEARN_DEBUG    - Set to "1" for debug logging
 */

import { writeFileSync } from "fs"
import { join } from "path"
import * as core from "./autolearn-core.mjs"

// @spec CM-GUARD-001
export const AutolearnPlugin = async (ctx) => {
  if (process.env.AUTOLEARN_DISABLED === "1") return {}
  if (process.env.AUTOLEARN_REVIEWER === "1") {
    core.dbg("SKIPPING: reviewer session, not counting turns")
    return {}
  }

  const { client, directory, worktree } = ctx
  core.ensureStore()

  // @spec CM-GUARD-002
  // Keyed by directory: opencode loads the plugin once per directory in a
  // single process, so a single global symbol made the FIRST-loaded project
  // the only monitor while the active project's instance saw the guard set
  // and went silent (issue #14). Per-directory keys keep the anti-duplicate-
  // load protection for same-directory double-loads while letting each
  // monitored project's instance be primary in its own right.
  const guard = Symbol.for("opencode:autolearn:" + (directory || worktree || process.cwd()))
  const isPrimary = !globalThis[guard]
  if (isPrimary) globalThis[guard] = true
  if (!isPrimary) {
    core.dbg("SKIPPING: secondary plugin instance for this directory, guard already set")
    return {}
  }

  core.dbg("PLUGIN LOADED (v1)", { isPrimary, hasClient: !!client, hasSession: !!(client?.session), directory })

  let turnCount = 0
  let userMsgCount = 0
  let lastReviewUserMsg = 0
  let lastIdleReview = 0
  let buffer = []
  let currentSessionId = null
  let reviewInProgress = false

  const messageTexts = new Map()
  const messageRoles = new Map()

  const projectName = () => (directory || worktree || process.cwd()).split("/").pop() || "unknown"
  const reviewCwd = () => directory || worktree || process.cwd()

  core.injectInstructions()
  core.composeContext()

  // `config` must be mutable — re-read at every trigger point so live edits
  // to config.yaml take effect without a plugin reload.
  let config = core.parseConfig()

  // @spec CM-RS-003, CM-RS-004, CM-RS-005
  async function spawnReview(trigger) {
    if (buffer.length === 0 || reviewInProgress) return
    const reviewText = buffer.map(m => m.content).join(" ")
    if (reviewText.includes(core.REVIEW_HEADING)) {
      core.dbg("SKIPPING: buffer contains review content (depth guard)")
      buffer = []
      return
    }
    // Speculate on the review content BEFORE clearing the buffer: if the
    // throttle denies the spawn (busy window / duplicate), the buffer stays
    // intact and this content rides the NEXT trigger instead of being lost.
    // PEEK ONLY (commit: false): committing here would write the lock that
    // the authoritative gate in runReviewSubprocess then matches against,
    // suppressing every review (issue #14).
    const reviewMd = core.formatReview(buffer, { project: projectName(), trigger })
    if (!core.throttleCheck(reviewMd, false)) {
      core.dbg("REVIEW QUEUED by throttle (v1)", buffer.length, "messages, trigger", trigger)
      return
    }

    reviewInProgress = true
    // @spec CM-BUF-003
    const captured = [...buffer]
    buffer = []

    core.dbg("SPAWN REVIEW", captured.length, "messages, trigger", trigger)

    try {
      core.runReviewSubprocess({
        reviewMd,
        title: "autolearn review",
        cwd: reviewCwd(),
        env: { AUTOLEARN_OPENCODE_BIN: "opencode" },
        messageCount: captured.length,
        project: projectName(),
        trigger,
      })

      // @spec CM-RS-014
      core.cleanStaleReviews(config)
    } catch (err) {
      // @spec CM-RS-011, CM-RS-012
      core.dbg("REVIEW SPAWN FAILED", err.message)
      console.error("[autolearn] Review spawn failed:", err.message)
      const fallback = join(core.AL_HOME, `review-failed-${Date.now()}.md`)
      writeFileSync(fallback, reviewMd)
    } finally {
      // @spec CM-RS-005
      reviewInProgress = false
    }
  }

  // @spec CM-RS-016, CM-RS-017, CM-RS-018, CM-RS-019
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
        cwd: reviewCwd(),
        env: { AUTOLEARN_OPENCODE_BIN: "opencode" },
        log: false, // original exit path did not log an observation
      })
      core.dbg("EXIT REVIEW SPAWNED", captured.length, "messages")
    } catch (err) {
      core.dbg("EXIT REVIEW FAILED", err.message)
    }
  }

  // @spec CM-RS-016
  process.on("beforeExit", () => {
    core.dbg("beforeExit — spawning exit review")
    spawnExitReview()
  })

  // @spec CM-RS-017
  let exitHandlersInstalled = false
  if (!exitHandlersInstalled) {
    exitHandlersInstalled = true
    const signals = ["SIGINT", "SIGTERM"]
    for (const sig of signals) {
      process.on(sig, () => {
        core.dbg(`${sig} received — spawning exit review`)
        spawnExitReview()
        process.exit(0)
      })
    }
  }

  return {
    event: async ({ event }) => {
      try {
        const props = event.properties || {}

        switch (event.type) {
          case "session.created": {
            const info = props.info || {}
            currentSessionId = info.id || props.sessionID
            core.dbg("SESSION CREATED", currentSessionId)
            // @spec SYNC-PROTO-012
            core.syncBackground("pull")
            break
          }

          case "message.updated": {
            const info = props.info || {}
            const msgId = info.id
            const role = info.role

            if (!msgId || !role) break

            // @spec CM-TC-003
            messageRoles.set(msgId, role)

            const text = messageTexts.get(msgId) || ""
            if (text && role === "assistant") {
              // @spec CM-TC-005, CM-TC-001
              const content = core.redact(core.truncate(text, 2000))
              // @spec CM-TC-005, CM-TC-001 (assistant text buffered; kept
              // as an observability counter only — the threshold unit is
              // USER messages)
              turnCount++
              // @spec CM-BUF-001
              buffer.push({ role: "assistant", content, timestamp: new Date().toISOString() })
              core.dbg("ASSISTANT TURN", turnCount, content.length, "chars")

              // @spec CM-RS-001, CM-RS-002 (threshold counts USER messages;
              // checked at the assistant-completion exchange boundary so the
              // review covers complete exchanges)
              const threshold = config.review_threshold || core.THRESHOLD_DEFAULT
              if (userMsgCount - lastReviewUserMsg >= threshold) {
                lastReviewUserMsg = userMsgCount
                core.dbg("TRIGGERING REVIEW after user msg", userMsgCount)
                spawnReview("threshold").catch(e => {
                  core.dbg("SPAWN REVIEW UNHANDLED", e.message)
                  reviewInProgress = false
                })
              }
            } else if (text && role === "user") {
              // @spec CM-TC-004
              const content = core.redact(core.truncate(text, 1000))
              // @spec CM-BUF-001
              buffer.push({ role: "user", content, timestamp: new Date().toISOString() })
              // @spec CM-TC-007 (v1: the threshold unit is USER messages)
              userMsgCount++
              core.dbg("USER MESSAGE", content.length, "chars")
            }

            // @spec CM-BUF-002
            const maxBuf = config.max_conversation_buffer || 50
            if (buffer.length > maxBuf) buffer = buffer.slice(-maxBuf)

            messageTexts.delete(msgId)
            break
          }

          case "message.part.updated": {
            const part = props.part || {}
            const msgId = part.messageID
            if (!msgId || part.type !== "text") break

            const text = part.text || ""
            if (text && !messageTexts.has(msgId)) {
              messageTexts.set(msgId, text)
            }
            break
          }

          case "message.part.delta": {
            const msgId = props.messageID
            const delta = props.delta || ""
            if (!msgId || !delta) break

            // @spec CM-TC-002
            const existing = messageTexts.get(msgId) || ""
            messageTexts.set(msgId, existing + delta)
            break
          }

          // @spec CM-IDLE-001, CM-IDLE-002, CM-IDLE-003, CM-IDLE-004
          case "session.idle": {
            const now = Date.now()
            // Re-read config at idle time: OpenCode runs for weeks, so a
            // startup-cached config makes live triage edits invisible.
            config = core.parseConfig()
            const cooldown = config.idle_cooldown_ms || core.IDLE_COOLDOWN_MS
            core.dbg("SESSION IDLE buffer=", buffer.length, "reviewInProgress=", reviewInProgress, "cooldownRemaining=", Math.max(0, cooldown - (now - lastIdleReview)))
            if (
              config.session_review_on_idle !== false &&
              buffer.length > 2 &&
              !reviewInProgress &&
              now - lastIdleReview >= cooldown
            ) {
              lastIdleReview = now
              spawnReview("idle").catch(e => {
                core.dbg("IDLE REVIEW UNHANDLED", e.message)
                reviewInProgress = false
              })
            }
            break
          }
        }
      } catch (err) {
        core.dbg("EVENT ERROR", err.message)
        console.error("[autolearn] Event error:", err.message)
      }
    },
  }
}

export default AutolearnPlugin
