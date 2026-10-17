// Sidelit companion: reports every window's tabs to the daemon, and opens
// windows with a given set of tabs when the daemon restores one.
//
// It talks to the native host `io.github.kulaxa.sidelit` (sidelit-chrome-host),
// which relays to the daemon's socket. An open native port keeps this worker alive; if the
// host goes (daemon restart, host crash) we reconnect on an alarm.
//
// Incognito windows are never reported.

const HOST = "io.github.kulaxa.sidelit";
const DEBOUNCE_MS = 300;

let port = null;
let instance = null;
let timer = null;
// tab id -> {url, title} for tabs we opened: until a background tab loads, Chrome reports it
// with no URL at all (kept in session storage, so a worker restart does not forget them)
let opened = {};
chrome.storage.session.get("opened").then((got) => { opened = { ...(got.opened || {}), ...opened }; });

function remember(tabId, t) {
  opened[tabId] = { url: t.url, title: t.title || "" };
  chrome.storage.session.set({ opened });
}

async function instanceId() {
  if (instance) return instance;
  const got = await chrome.storage.local.get("instance");
  instance = got.instance;
  if (!instance) {
    instance = "c" + crypto.randomUUID().replace(/-/g, "").slice(0, 12);
    await chrome.storage.local.set({ instance });
  }
  return instance;
}

function send(msg) {
  if (!port) return;
  try {
    port.postMessage(msg);
  } catch (e) {
    port = null;
  }
}

let connecting = null;

function connect() {
  // startup, install and the first run can all ask at once: one port
  if (port) return Promise.resolve();
  if (!connecting) connecting = open().finally(() => { connecting = null; });
  return connecting;
}

async function open() {
  const id = await instanceId();
  if (port) return;
  try {
    port = chrome.runtime.connectNative(HOST);
  } catch (e) {
    port = null;
    return;
  }
  port.onMessage.addListener(onCommand);
  port.onDisconnect.addListener(() => {
    port = null; // chrome.runtime.lastError says why; the alarm tries again
    void chrome.runtime.lastError;
  });
  send({ type: "hello", instance: id, version: chrome.runtime.getManifest().version });
  report();
}

function tabInfo(t) {
  const known = opened[t.id];
  const url = t.pendingUrl || (t.url && t.url !== "about:blank" ? t.url : "") || (known && known.url) || t.url || "";
  return { url, title: t.title || (known && known.title) || "", active: t.active, pinned: t.pinned };
}

async function snapshot() {
  const wins = await chrome.windows.getAll({ populate: true, windowTypes: ["normal", "app", "popup"] });
  return wins
    .filter((w) => !w.incognito)
    .map((w) => ({ id: w.id, type: w.type, focused: w.focused, state: w.state, tabs: (w.tabs || []).map(tabInfo) }));
}

function report() {
  clearTimeout(timer);
  timer = setTimeout(async () => {
    send({ type: "windows", instance: await instanceId(), windows: await snapshot() });
  }, DEBOUNCE_MS);
}

function isBlank(t) {
  // not a tab we opened, and not one with no URL at all: that is a tab that has not loaded yet
  // (a window we just reopened looks all blank until its tabs load, and reusing it closes them)
  if (opened[t.id]) return false;
  const u = t.url || t.pendingUrl || "";
  return u === "about:blank" || u.startsWith("chrome://newtab") || u.startsWith("chrome://new-tab-page");
}

async function blankWindow() {
  const wins = await chrome.windows.getAll({ populate: true, windowTypes: ["normal"] });
  return wins.find((w) => !w.incognito && w.tabs && w.tabs.length && w.tabs.every(isBlank)) || null;
}

async function openWindow(cmd) {
  const tabs = (cmd.tabs || []).filter((t) => t.url);
  if (!tabs.length) throw new Error("no tabs to open");
  const activeIndex = Math.max(0, tabs.findIndex((t) => t.active));
  let win = cmd.reuse_blank ? await blankWindow() : null;
  let created;
  if (win) {
    const blanks = win.tabs.map((t) => t.id);
    created = [];
    for (const t of tabs) created.push(await chrome.tabs.create({ windowId: win.id, url: t.url, active: false }));
    created.forEach((c, i) => remember(c.id, tabs[i]));
    await chrome.tabs.remove(blanks);
  } else {
    win = await chrome.windows.create({ url: tabs.map((t) => t.url), focused: true });
    created = win.tabs || (await chrome.tabs.query({ windowId: win.id }));
    created.forEach((c, i) => { if (tabs[i]) remember(c.id, tabs[i]); });
  }
  for (let i = 0; i < created.length && i < tabs.length; i++) {
    if (tabs[i].pinned) await chrome.tabs.update(created[i].id, { pinned: true });
  }
  if (created[activeIndex]) await chrome.tabs.update(created[activeIndex].id, { active: true });
  // (no tabs.discard here: a tab discarded before its page commits forgets its URL and
  // turns into about:blank; Chrome's own Memory Saver puts idle tabs to sleep safely)
  return win.id;
}

// one window opened at a time: two at once could both take the same blank window
let opening = Promise.resolve();

async function onCommand(cmd) {
  if (cmd.cmd === "snapshot") {
    report();
  } else if (cmd.cmd === "open") {
    try {
      const turn = opening.then(() => openWindow(cmd));
      opening = turn.catch(() => {});
      const id = await turn;
      send({ type: "opened", req: cmd.req, window: id });
    } catch (e) {
      send({ type: "error", req: cmd.req, error: String(e && e.message ? e.message : e) });
    }
    report();
  }
}

for (const ev of [
  chrome.windows.onCreated, chrome.windows.onRemoved, chrome.windows.onFocusChanged,
  chrome.tabs.onCreated, chrome.tabs.onRemoved, chrome.tabs.onMoved, chrome.tabs.onActivated,
  chrome.tabs.onAttached, chrome.tabs.onDetached, chrome.tabs.onReplaced,
]) {
  ev.addListener(report);
}
chrome.tabs.onUpdated.addListener((id, change) => {
  if (change.url && change.url !== "about:blank" && opened[id]) {
    delete opened[id]; // loaded: Chrome knows its URL now
    chrome.storage.session.set({ opened });
  }
  if (change.url || change.title || change.pinned !== undefined || change.status === "complete") report();
});
chrome.tabs.onRemoved.addListener((id) => {
  if (opened[id]) {
    delete opened[id];
    chrome.storage.session.set({ opened });
  }
});

chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
chrome.alarms.create("reconnect", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(() => connect());
connect();
