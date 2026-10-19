// Sidelit's OpenCode plugin: tells the Sidelit sidebar what each OpenCode session is doing.
//
// `sidelit hooks install` copies this file into OpenCode's plugin folder
// (~/.config/opencode/plugins/), from which OpenCode loads it by itself. OpenCode 1 (1.18.29 and
// later) calls its `server()` in the process of each opencode it starts: a terminal's, or
// `opencode run`'s. OpenCode 2 calls its `setup()` in its server: the background one all its
// terminals share (or a terminal's own, with --standalone), once for each folder it works in. For
// each session it writes the same record Sidelit's hook writes for Claude Code and Codex
// (plugin/hooks/hook.py) to $XDG_RUNTIME_DIR/sidelit/opencode/<session>.json, and nudges Sidelit's
// daemon over its socket.
//
// What OpenCode's events mean is in EVENTS and READ, and nowhere else. OpenCode 1 says what an
// event is about under `properties`, OpenCode 2 under `data`, and names some anew (a turn is
// session.execution.*, a question a form): both are in the tables.
//
// OpenCode 2's server cannot tell which terminal shows a session, so its records say they come
// from the server (`server`), and Sidelit finds the opencode in a terminal that shows the session
// (sidelit/opencode.py).
//
// It must never get in OpenCode's way: no dependencies, nothing thrown, no output.

import { mkdirSync, readFileSync, renameSync, writeFileSync } from "fs"
import { createConnection } from "net"
import { join } from "path"
import { createHash } from "crypto"

// event -> the session's state afterwards ("busy" working, "waiting" for you, "done" its turn
// is over, "ended"); a function gets what the event says. Missing: the event changes nothing.
const EVENTS = {
  // OpenCode 1
  "session.status": (p) => (p.status && p.status.type === "idle" ? "done" : "busy"), // idle | busy | retry
  "session.idle": () => "done", // (deprecated, the same as session.status idle)
  "session.error": () => "done",
  "permission.asked": () => "waiting", // (also OpenCode 2's)
  "permission.replied": () => "busy", // (also OpenCode 2's)
  "question.asked": () => "waiting",
  "question.replied": () => "busy",
  "question.rejected": () => "busy",
  "session.deleted": () => "ended", // (also OpenCode 2's)
  // OpenCode 2
  "session.execution.started": () => "busy",
  "session.execution.succeeded": () => "done",
  "session.execution.interrupted": () => "done",
  "session.execution.failed": () => "done",
  "form.created": () => "waiting", // (a question for you)
  "form.replied": () => "busy",
  "form.cancelled": () => "busy",
}
// the other events read: the session's title and folder, what you typed, the model, the answer
const READ = new Set([
  // OpenCode 1
  "session.created", "session.updated", "message.updated", "message.part.updated",
  // OpenCode 2 (its session.created says it all at the top)
  "session.renamed", "session.inbox.enqueued", "session.step.started", "session.text.ended",
])
const SAID_MAX = 6000 // characters of the last answer kept with the turn's end
const ASKED_MAX = 1500 // characters of your request
const TOOLS_MAX = 8 // tool calls of the turn kept
const TOOL_MAX = 160 // characters of each
const PANE_VARS = ["TERMINATOR_UUID", "KITTY_WINDOW_ID", "KITTY_LISTEN_ON", "WEZTERM_PANE", "WEZTERM_UNIX_SOCKET",
  "TMUX_PANE", "TMUX"]
const DEFAULT_TITLE = /^(New|Child) session - \d{4}-/ // (what OpenCode calls one before it has a title)

const env = process.env
const home = env.SIDELIT_HOME
const runtime = home || join(env.XDG_RUNTIME_DIR || "/tmp", "sidelit")
const folder = join(runtime, "opencode")
const socket = home
  ? join(env.XDG_RUNTIME_DIR || "/tmp", `sidelit-${createHash("sha1").update(home).digest("hex").slice(0, 8)}.sock`)
  : join(runtime, "daemon.sock")

// What every copy of the plugin in this process shares: OpenCode 2 loads it once per folder, and
// each copy hears every session's events. (An update loads the new file into the same process:
// what it shares that an older one did not, it adds.)
const shared = globalThis.__sidelitOpenCode || (globalThis.__sidelitOpenCode = {})
for (const [key, value] of Object.entries({
  v1: false, // (OpenCode 1 called server(): its hooks hear every event)
  server: false, // (OpenCode 2: this process is a server, not the terminal that shows the session)
  parents: new Map(), // a subagent's session -> its parent's (the last KEPT)
  texts: new Map(), // OpenCode 1: a message -> its role, and the text of its parts so far (the last MESSAGES)
  seen: new Set(), // OpenCode 2: the events and tool calls handled (the last KEPT)
})) {
  if (!(key in shared)) shared[key] = value
}
const KEPT = 2000
const MESSAGES = 200

// what the event says: OpenCode 1 under `properties`, 2 under `data`
const said = (event) => event.properties || event.data || {}
// which session it is about
const sessionOf = (p) => p.sessionID || (p.form && p.form.sessionID) || (p.info && p.info.sessionID) ||
  (p.part && p.part.sessionID) || (p.info && p.info.id) || ""

const clip = (text, limit) => {
  text = String(text || "").split(/\s+/).join(" ").trim()
  return text.length <= limit ? text : text.slice(0, limit - 1) + "…"
}

function first(key) {
  // (true the first time: an event every copy of the plugin hears is handled once)
  if (shared.seen.has(key)) return false
  shared.seen.add(key)
  if (shared.seen.size > KEPT) shared.seen.delete(shared.seen.values().next().value)
  return true
}

const wanted = (event) => event && (event.type in EVENTS || READ.has(event.type))

function ancestry() {
  const chain = []
  let pid = process.pid
  for (let i = 0; i < 40 && pid > 1; i++) {
    try {
      const stat = readFileSync(`/proc/${pid}/stat`, "utf8")
      chain.push(pid)
      pid = parseInt(stat.slice(stat.lastIndexOf(")") + 2).split(" ")[1])
    } catch {
      break
    }
  }
  return chain
}

// whose process this is: OpenCode 1's own, in its terminal (its pane, where the terminal says);
// OpenCode 2's server, which shows nothing
function where(rec) {
  if (shared.server) {
    Object.assign(rec, { server: true, pid: 0 })
    delete rec.ancestors
    delete rec.pane
    return
  }
  Object.assign(rec, { pid: process.pid, ancestors: ancestry() })
  const pane = {}
  for (const k of PANE_VARS) if (env[k]) pane[k] = String(env[k]).slice(0, 200)
  if (Object.keys(pane).length) rec.pane = pane
}

function remember(map, key, value, kept) {
  // (the newest last; the oldest goes past `kept`)
  map.delete(key)
  map.set(key, value)
  if (map.size > kept) map.delete(map.keys().next().value)
}

function load(sid) {
  try {
    return JSON.parse(readFileSync(join(folder, sid + ".json"), "utf8"))
  } catch {
    return {}
  }
}

function save(sid, rec) {
  try {
    mkdirSync(folder, { recursive: true, mode: 0o700 })
    const path = join(folder, sid + ".json")
    const tmp = `${path}.${process.pid}.tmp`
    writeFileSync(tmp, JSON.stringify(rec))
    renameSync(tmp, path)
  } catch {}
  try {
    const s = createConnection(socket)
    s.on("error", () => {})
    s.setTimeout(300, () => s.destroy())
    s.end(JSON.stringify({ method: "agent_event", params: { agent: "opencode", session: sid } }) + "\n")
  } catch {}
}

function typed(sid, rec, id, text, now) {
  // what you typed (once per message: OpenCode 1 sends the part again as it goes)
  if (rec.asked_id === id) return
  rec.asked_id = id
  rec.asked = String(text).trim().slice(0, ASKED_MAX)
  rec.tools = []
  delete rec.said_part // (a turn that ends before it says anything must not end with the last one's words)
  delete rec.said_msg
  if (!rec.first_prompt) rec.first_prompt = clip(text, 120)
  try {
    mkdirSync(folder, { recursive: true, mode: 0o700 }) // (a session's first prompt may come before its record)
    writeFileSync(join(folder, sid + ".prompt"), String(now / 1000))
  } catch {}
}

function handle(event, directory) {
  const type = event.type || ""
  if (!wanted(event)) return // (text as it streams, diffs, ...: nothing for us)
  const p = said(event)
  const info = p.info || {}
  if (type === "session.created" || type === "session.updated") {
    const id = info.id || p.sessionID
    const parent = info.parentID || p.parentID
    if (id && parent) remember(shared.parents, id, parent, KEPT)
  }
  const sid = sessionOf(p)
  if (!sid) return
  const now = Date.now()
  const parent = shared.parents.get(sid)
  if (parent) {
    // a subagent's session: its parent's list of them at work changes, and a permission or a
    // question it asks waits for you all the same
    const state = EVENTS[type] && EVENTS[type](p)
    const rec = state ? load(parent) : {}
    if (!rec.session) return
    rec.agents = rec.agents && typeof rec.agents === "object" ? rec.agents : {}
    if (state === "waiting") {
      if (rec.state !== "waiting") rec.resume = rec.state || "busy"
      rec.state = "waiting"
    } else {
      if (state === "busy") rec.agents[sid] = now / 1000
      else delete rec.agents[sid]
      if (rec.state === "waiting" && "resume" in rec) {
        rec.state = rec.resume // (answered: back to what the main session did)
        delete rec.resume
      }
    }
    rec.t = now / 1000
    save(parent, rec)
    return
  }
  const rec = load(sid)
  const fresh = !rec.session
  Object.assign(rec, { session: sid, event: type, t: now / 1000 })
  where(rec)
  const at = (p.location && p.location.directory) || (event.location && event.location.directory)
  rec.cwd = info.directory || at || rec.cwd || (shared.server ? "" : directory) || ""
  if (fresh) {
    rec.started = now / 1000
    rec.state = rec.state || "done"
    rec.agents = {}
  }
  const title = type === "session.created" || type === "session.updated" ? info.title || p.title
    : type === "session.renamed" ? p.title : ""
  if (title && !DEFAULT_TITLE.test(title)) rec.title = clip(title, 120)
  // the model: OpenCode 1 says it with each answer, 2 with each step (and the session's start)
  if (type === "message.updated" && info.role === "assistant" && info.modelID) rec.model = String(info.modelID).slice(0, 80)
  if ((type === "session.step.started" || type === "session.created") && p.model && p.model.id) {
    rec.model = String(p.model.id).slice(0, 80)
  }
  if (type === "message.updated" && info.role) {
    const msg = shared.texts.get(info.id) || { role: info.role, parts: new Map() }
    msg.role = info.role
    remember(shared.texts, info.id, msg, MESSAGES)
  }
  if (type === "message.part.updated" && p.part) {
    const part = p.part
    const msg = shared.texts.get(part.messageID) || { role: "", parts: new Map() }
    remember(shared.texts, part.messageID, msg, MESSAGES)
    if (part.type === "text" && typeof part.text === "string" && !part.synthetic) msg.parts.set(part.id, part.text)
    if (msg.role === "user" && part.type === "text" && part.text && !part.synthetic) typed(sid, rec, part.messageID, part.text, now)
    if (msg.role === "assistant" && part.type === "text") {
      rec.said_part = [...msg.parts.values()].join("\n").slice(-SAID_MAX)
    }
  }
  if (type === "session.inbox.enqueued" && p.item && p.item.type === "user" && p.item.payload && p.item.payload.text) {
    typed(sid, rec, p.inboxID, p.item.payload.text, now)
  }
  if (type === "session.text.ended" && typeof p.text === "string") {
    // (the answer's text blocks, as each ends: the last answer's are kept)
    const more = rec.said_msg === p.assistantMessageID && rec.said_part
    rec.said_part = (more ? rec.said_part + "\n" + p.text : p.text).slice(-SAID_MAX)
    rec.said_msg = p.assistantMessageID
  }
  const state = EVENTS[type] && EVENTS[type](p)
  if (state === "busy" && rec.state !== "busy") {
    delete rec.turn_end
    delete rec.said
  }
  if (state === "done" && rec.state !== "done") {
    // the turn ended: what the daemon may ask a model about (turncheck.py), with its last words
    rec.turn_end = now / 1000
    if (rec.said_part && rec.said_part.trim()) rec.said = rec.said_part
    else delete rec.said
  }
  if (state) {
    rec.state = state
    delete rec.resume // (the main session's own word: what a subagent's question had put aside is past)
  }
  save(sid, rec)
}

function tool(sid, name, args) {
  if (!sid || shared.parents.has(sid)) return
  const rec = load(sid)
  if (!rec.session) return
  const what = args && (args.command || args.description || args.filePath || args.path || args.url || args.pattern)
  const line = clip(`${name || "tool"}${what ? ": " + what : ""}`, TOOL_MAX)
  rec.tools = [...(Array.isArray(rec.tools) ? rec.tools : []).slice(-(TOOLS_MAX - 1)), line]
  rec.t = Date.now() / 1000
  save(sid, rec)
}

// OpenCode 1: a session this opencode was started to show (`opencode -s <id>`: how Sidelit
// resumes one after a login) says nothing until you type in it: it is on screen from the start,
// done. (OpenCode 2's server cannot tell: Sidelit reads the terminal's opencode itself.)
function opened(directory) {
  // (the process's own command line: OpenCode runs its plugins in a worker of its own arguments)
  let argv = process.argv || []
  try {
    argv = readFileSync(`/proc/${process.pid}/cmdline`, "utf8").split("\0").filter(Boolean)
  } catch {}
  if (argv.includes("--fork") || argv.includes("run")) return // (a new session; one that is not on screen)
  let sid = ""
  for (let i = 0; i < argv.length; i++) {
    if ((argv[i] === "-s" || argv[i] === "--session") && argv[i + 1]) sid = argv[i + 1]
    else if (argv[i].startsWith("--session=")) sid = argv[i].slice("--session=".length)
  }
  if (!/^[\w-]{1,100}$/.test(sid)) return
  const now = Date.now() / 1000
  const rec = load(sid)
  Object.assign(rec, { session: sid, event: "opened", t: now, started: now, cwd: rec.cwd || directory || "" })
  where(rec)
  rec.state = "done"
  rec.agents = {}
  save(sid, rec)
}

export default {
  id: "sidelit",

  // OpenCode 1: its hooks
  server: async ({ directory } = {}) => {
    shared.v1 = true
    try {
      opened(directory)
    } catch {}
    return {
      event: async ({ event }) => {
        try {
          handle(event || {}, directory)
        } catch {}
      },
      "tool.execute.before": async (input, output) => {
        try {
          tool(input && input.sessionID, input && input.tool, output && output.args)
        } catch {}
      },
    }
  },

  // OpenCode 2: every event of its server, and each tool call before it runs. (OpenCode 1 calls
  // it too, after server(), with neither: its hooks above do it all there.)
  setup: async (ctx) => {
    if (shared.v1 || !ctx || !ctx.event || typeof ctx.event.subscribe !== "function" || !ctx.tool) return () => {}
    shared.server = true
    const stop = new AbortController()
    try {
      ;(async () => {
        for await (const event of ctx.event.subscribe({ signal: stop.signal })) {
          try {
            if (wanted(event) && first(event.id || JSON.stringify(event))) handle(event, "")
          } catch {}
        }
      })().catch(() => {})
      await ctx.tool.hook("execute.before", (call) => {
        try {
          if (call && first("tool " + (call.id || Math.random()))) tool(call.sessionID, call.tool, call.input)
        } catch {}
      })
    } catch {}
    return () => stop.abort()
  },
}
