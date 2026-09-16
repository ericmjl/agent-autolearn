/**
 * Autolearn Plugin — OpenCode v2 (beta) shell.
 *
 * Same self-improvement loop as the v1 plugin (plugin/autolearn.js), adapted
 * to the v2 plugin API: a plain-object plugin whose setup() subscribes to the
 * server's event stream. All shared logic lives in ./autolearn-core.mjs.
 *
 * Deliberately imports NOTHING beyond Node/Bun builtins and the local core —
 * the v2 plugin package (@opencode-ai/plugin@beta) is not required at
 * runtime, so this file resolves in any project without node_modules.
 *
 * v2 event shapes handled (verified against opencode2 v0.0.0-beta-18684):
 *   session.created            { sessionID, title, parentID }
 *   session.inbox.enqueued     { sessionID, inboxID, item: { type, payload: { text } } }
 *   session.step.started       { sessionID, assistantMessageID, agent, model }
 *   session.text.ended         { sessionID, assistantMessageID, text }
 *   session.step.ended         { sessionID, assistantMessageID, finish }
 *   session.execution.*        { sessionID } (started/succeeded/failed/interrupted)
 *   session.idle               { sessionID } (when the server emits it)
 *
 * The v2 event stream is location-scoped: each plugin instance receives
 * events for sessions in its own location, so state is tracked per session
 * without a global primary-instance guard (unlike v1).
 *
 * Environment variables: same as v1 (AUTOLEARN_HOME, AUTOLEARN_DISABLED,
 * AUTOLEARN_DEBUG).
 */

import * as core from "./autolearn-core.mjs"

const REVIEWER_AGENTS = new Set(["autolearn-reviewer"])
// Exact titles the plugin itself passes to `run --title` (a loose prefix
// like "autolearn" would misclassify ordinary sessions in this repo).
const REVIEWER_TITLES = new Set(["autolearn review", "autolearn exit review"])
const MAX_TRACKED_SESSIONS = 50
const MAX_PENDING_TEXTS = 200

export default {
  id: "autolearn",
  async setup(ctx) {
    if (process.env.AUTOLEARN_DISABLED === "1") return
    if (process.env.AUTOLEARN_REVIEWER === "1") {
      core.dbg("SKIPPING (v2): reviewer session, not counting turns")
      return
    }

    core.ensureStore()
    let config = core.parseConfig()
    const directory = ctx?.location?.directory || ctx?.directory || process.cwd()
    const projectName = () => String(directory).split("/").pop() || "unknown"

    core.dbg("PLUGIN LOADED (v2)", { directory, version: ctx?.app?.version })

    core.injectInstructions()
    core.composeContext()
    core.syncBackground("pull")

    // per-session review state (location-scoped stream)
    const sessions = new Map()
    // assistantMessageID → full text, captured on session.text.ended
    const assistantTexts = new Map()
    let reviewInProgress = false

    function state(sid) {
      let st = sessions.get(sid)
      if (st) {
        // refresh recency so eviction is LRU, not FIFO (the long-lived
        // primary session must never be evicted in favor of children)
        sessions.delete(sid)
        sessions.set(sid, st)
        return st
      }
      st = {
        buffer: [],
        userMsgCount: 0,
        lastReviewUserMsg: 0,
        lastIdleReview: 0,
        isReviewer: false,
        lastInboxID: null,
      }
      sessions.set(sid, st)
      if (sessions.size > MAX_TRACKED_SESSIONS) {
        sessions.delete(sessions.keys().next().value)
      }
      return st
    }

    function trimBuffer(st) {
      const maxBuf = config.max_conversation_buffer || 50
      if (st.buffer.length > maxBuf) st.buffer = st.buffer.slice(-maxBuf)
    }

    // @spec CM-RS-003, CM-RS-004, CM-RS-005 (v2: turn boundary instead of idle-only)
    async function spawnReview(st, trigger) {
      if (st.buffer.length === 0 || reviewInProgress) return
      const reviewText = st.buffer.map(m => m.content).join(" ")
      if (reviewText.includes(core.REVIEW_HEADING)) {
        core.dbg("SKIPPING: buffer contains review content (depth guard)")
        st.buffer = []
        return
      }
      // Speculate on the review content BEFORE clearing the buffer: if the
      // throttle denies the spawn (busy window / duplicate), the buffer stays
      // intact and this content rides the NEXT trigger instead of being lost.
      // PEEK ONLY (commit: false): committing here would write the lock that
      // the authoritative gate in runReviewSubprocess then matches against,
      // suppressing every review (issue #14).
      const reviewMd = core.formatReview(st.buffer, { project: projectName(), trigger })
      if (!core.throttleCheck(reviewMd, false)) {
        core.dbg("REVIEW QUEUED by throttle (v2)", st.buffer.length, "messages, trigger", trigger)
        return
      }

      reviewInProgress = true
      // @spec CM-BUF-003
      const captured = [...st.buffer]
      st.buffer = []

      core.dbg("SPAWN REVIEW (v2)", captured.length, "messages, trigger", trigger)

      try {
        core.runReviewSubprocess({
          reviewMd,
          title: "autolearn review",
          cwd: directory,
          // Pin the wrapper to the v2 binary so reviews run on the same
          // OpenCode version that spawned them (v2 has no `session delete`
          // CLI; the wrapper uses the HTTP API for opencode2).
          env: { AUTOLEARN_OPENCODE_BIN: "opencode2" },
          messageCount: captured.length,
          project: projectName(),
          trigger,
        })
        core.cleanStaleReviews(config)
      } catch (err) {
        // @spec CM-RS-011, CM-RS-012
        core.dbg("REVIEW SPAWN FAILED (v2)", err.message)
        console.error("[autolearn] Review spawn failed:", err.message)
        try {
          const { writeFileSync: wfs } = await import("fs")
          const { join: jn } = await import("path")
          wfs(jn(core.AL_HOME, `review-failed-${Date.now()}.md`), reviewMd)
        } catch {}
      } finally {
        // @spec CM-RS-005
        reviewInProgress = false
      }
    }

    // @spec CM-IDLE-001..004 (v2: execution end is the turn boundary)
    function maybeIdleReview(st) {
      const now = Date.now()
      const cooldown = config.idle_cooldown_ms || core.IDLE_COOLDOWN_MS
      if (
        config.session_review_on_idle !== false &&
        st.buffer.length > 2 &&
        !reviewInProgress &&
        now - st.lastIdleReview >= cooldown
      ) {
        st.lastIdleReview = now
        spawnReview(st, "idle").catch(e => {
          core.dbg("IDLE REVIEW UNHANDLED (v2)", e.message)
          reviewInProgress = false
        })
      }
    }

    function handleEvent(event) {
      const type = event?.type
      const data = event?.data ?? event?.properties ?? {}
      const sid = data.sessionID

      switch (type) {
        case "session.created": {
          // Review subprocess sessions are top-level sessions in this
          // service's event stream; mark them so we never count or review
          // our own reviewer output (v1 used a subprocess env guard, but in
          // v2 the plugin runs inside the shared service).
          if (sid) {
            const st = state(sid)
            if (typeof data.title === "string" && REVIEWER_TITLES.has(data.title)) {
              st.isReviewer = true
            }
          }
          // @spec SYNC-PROTO-012
          core.syncBackground("pull")
          break
        }

        // @spec CM-TC-004 (v2: user text arrives via the inbox)
        case "session.inbox.enqueued": {
          if (!sid) break
          const st = state(sid)
          if (st.isReviewer) break
          if (data.inboxID && st.lastInboxID === data.inboxID) break
          if (data.inboxID) st.lastInboxID = data.inboxID

          const item = data.item
          if (item?.type !== "user") break
          const text = item?.payload?.text
          if (!text) break

          const content = core.redact(core.truncate(text, 1000))
          st.buffer.push({ role: "user", content, timestamp: new Date().toISOString() })
          trimBuffer(st)
          st.userMsgCount++
          core.dbg("USER MESSAGE (v2)", sid, content.length, "chars")
          break
        }

        case "session.step.started": {
          if (!sid) break
          const st = state(sid)
          if (data.agent && REVIEWER_AGENTS.has(data.agent)) st.isReviewer = true
          break
        }

        // @spec CM-TC-002 (v2: full text delivered at part end; a message
        // with several text parts accumulates them all)
        case "session.text.ended": {
          if (!sid || !data.assistantMessageID) break
          if (state(sid).isReviewer) break
          if (typeof data.text === "string" && data.text) {
            if (assistantTexts.size > MAX_PENDING_TEXTS) {
              assistantTexts.delete(assistantTexts.keys().next().value)
            }
            const id = data.assistantMessageID
            const prev = assistantTexts.get(id)
            assistantTexts.set(id, prev ? `${prev}\n\n${data.text}` : data.text)
          }
          break
        }

        // @spec CM-TC-005 (v2: assistant text buffered at step end; the
        // threshold counter is USER messages — see CM-TC-004/CM-RS-001).
        // Dedupe is the assistantTexts entry itself: a step that produced no
        // text (tool-only steps of the same message) finds nothing to count,
        // and consuming the entry prevents recounting on later steps.
        case "session.step.ended": {
          if (!sid || !data.assistantMessageID) break
          const st = state(sid)
          if (st.isReviewer) break

          const text = assistantTexts.get(data.assistantMessageID)
          if (!text) break
          assistantTexts.delete(data.assistantMessageID)

          const content = core.redact(core.truncate(text, 2000))
          st.buffer.push({ role: "assistant", content, timestamp: new Date().toISOString() })
          trimBuffer(st)
          core.dbg("ASSISTANT TURN (v2)", sid, content.length, "chars")

          // @spec CM-RS-001, CM-RS-002 (v2: the threshold counts USER
          // messages; the check runs here because assistant completion is
          // the exchange boundary — the review always covers complete
          // exchanges. Per-session spacing comes from min_interval_ms via
          // the global throttle, so 8 project instances don't fire
          // simultaneously.)
          const threshold = config.review_threshold || core.THRESHOLD_DEFAULT
          if (st.userMsgCount - st.lastReviewUserMsg >= threshold) {
            st.lastReviewUserMsg = st.userMsgCount
            core.dbg("TRIGGERING REVIEW (v2) after user msg", st.userMsgCount)
            // Re-read config at trigger time: OpenCode runs for weeks, and a
            // startup-cached config makes live triage edits invisible.
            config = core.parseConfig()
            spawnReview(st, "threshold").catch(e => {
              core.dbg("SPAWN REVIEW UNHANDLED (v2)", e.message)
              reviewInProgress = false
            })
          }
          break
        }

        // @spec CM-IDLE-001..004 (v2: execution end / idle as quiet signal)
        case "session.execution.succeeded":
        case "session.execution.failed":
        case "session.execution.interrupted":
        case "session.idle": {
          if (!sid) break
          const st = state(sid)
          if (st.isReviewer) break
          // Re-read config at idle time too (same rationale as the threshold path).
          config = core.parseConfig()
          maybeIdleReview(st)
          break
        }
      }
    }

    const controller = new AbortController()
    void (async () => {
      try {
        for await (const event of ctx.event.subscribe({ signal: controller.signal })) {
          try {
            handleEvent(event)
          } catch (err) {
            core.dbg("EVENT ERROR (v2)", err.message)
            console.error("[autolearn] Event error:", err.message)
          }
        }
      } catch (err) {
        core.dbg("EVENT STREAM ENDED (v2)", err?.message)
      }
    })()

    return () => {
      controller.abort()
      core.dbg("PLUGIN UNLOADED (v2)")
    }
  },
}
