// chatdash v2 entry: feeds, render loop, keyboard, theme, focus mode, tab badge.
import { renderAccounts } from "./accounts.js";
import { Feed, put, get, TOKEN } from "./api.js";
import { h, store, toast, age } from "./lib.js";
import { renderRail, renderNeeds, renderSide, healthOf, splitNeeds } from "./board.js";
import { renderCapacity } from "./capacity.js";
import { Graph } from "./graph.js";
import { spawnDialog } from "./spawn.js";
import { post } from "./api.js";
import { send, configure } from "./answer.js";
import { draftOf, partsOf, bodyFor, recommendedBody } from "./cards.js";
import { renderProposals, renderWorkItem } from "./workitem.js";
import { Palette } from "./palette.js";
import { Voice } from "./voice.js";
import { ChatView } from "./chat.js";
import { renderSettings, renderSessions } from "./settings.js";
import { initNebula, currentLook } from "./nebula.js";
import { Sky } from "./sky.js";

const ui = {
  group: store.get("group", "work item"),
  expanded: new Set(),
  focus: { on: false, min_age_min: 15, windows: ["10:00", "14:00"] },
  lastLooked: store.get("lastLooked", null),
  cur: 0,
  items: [],
  setGroup(m) { ui.group = m; store.set("group", m); render(); },
  expand(k) { ui.expanded.add(k); render(); },
  cycleTheme, toggleFocusPanel, showKeys,
  drafts: new Map(),
  receipts: new Map(),
  review: false,
  send: (item, body, label) => send(item, body, label),
  rerender: () => render(),
  markCur: () => markCur(false),
  toggleReview: () => { ui.review = !ui.review; ui.cur = 0; render(); },
  confirmLowRisk, askEvidence,
  store,
  spawn: (opts) => spawnDialog({ ...opts, seats: (overview.data && overview.data.capacity.seats) || [], onDone: () => { overview.now(); graph.now(); } }),
  itemFor: (sid) => (overview.data ? overview.data.needs_you.find((x) => x.session_id === sid) : null),
  jumpTo: (sid) => { location.hash = "#/"; setTimeout(() => { const i = ui.items.findIndex((x) => x.session_id === sid); if (i >= 0) { ui.cur = i; ui.keyed = true; markCur(true); } }, 50); },
  terminal: async (n) => { try { await post("sessions/" + encodeURIComponent(n.session_id) + "/terminal"); toast("Terminal opened"); } catch (e) { toast(`Not opened: ${e.message}`); } },
  stopSession: async (n) => {
    if (!confirm(`Stop ${n.label}? The background session ends; its transcript stays.`)) return;
    try { await post("sessions/" + encodeURIComponent(n.session_id) + "/stop"); toast("Stopped"); graph.now(); } catch (e) { toast(`Not stopped: ${e.message}`); }
  },
  markDone: async (n) => {
    try { await post("sessions/" + encodeURIComponent(n.session_id) + "/done"); toast("Handoff proposed: confirm it on the board"); overview.now(); }
    catch (e) { toast(`Not proposed: ${e.message}${e.data && e.data.milestone ? ` (arrives in ${e.data.milestone})` : ""}`); }
  },
  overview: () => overview.data,
  graphData: () => graph.data,
  refresh: () => { overview.now(); graph.now(); },
  openNode: (id) => { if (gview && gview.m) { const n = gview.m.byId.get(id); if (n) { gview.focus = id; gview.open(n); } } },
  useDraft: async (x, dr) => {
    const d = draftOf(ui, x.id);
    d.reply = dr.text;
    render();
    const ta = document.querySelector(`[data-fk="${CSS.escape(x.id + ":reply")}"]`);
    if (ta) { ta.value = dr.text; ta.focus(); }
    try { await post(`suggestions/${dr.id}/used`); } catch { /* the count is a nicety */ }
    toast("Draft placed in the reply box; edit it, then Reply");
  },
  runIntent,
  route: () => route(),
  toggleFocusPanel: () => toggleFocusPanel(),
  cycleTheme: () => cycleTheme(),
  setGate: async (x, on) => {
    try { await put("gate/" + encodeURIComponent(x.session_id), { on }); toast(`Evidence gate ${on ? "on" : "off"} for ${x.title}`); overview.now(); }
    catch (e) { toast(`Gate not changed: ${e.message}`); }
  },
};
configure({ refresh: () => overview.now(), sent: (item) => ui.drafts.delete(item.id) });

const rail = document.getElementById("rail");
const main = document.getElementById("main");
const side = document.getElementById("side");
const cap = document.getElementById("cap");

// A poll whose data did not change (only clocks and ages moved) repaints the rail, the strip ages and the badge, not
// every section: rebuilding capacity, proposals, sessions and the field each 5 s was the board's main idle cost.
const QUIET = new Set(["generated_at", "snapshot_at", "seconds", "meter_at"]);
const dataSig = (f) => (f.error ? "E" : "") + JSON.stringify(f.data, (k, v) => (QUIET.has(k) ? undefined : v));
let ovSig = "", ovFullAt = 0, grSig = "";
function onOverview(f) {
  const s = dataSig(f);
  if (s === ovSig && Date.now() - ovFullAt < 30000) {
    renderRail(rail, f.data, overview, ui);
    ui.items = needsOrFirstRun(f.data);
    markCur(false);
    badge(f.data);
    return;
  }
  ovSig = s; ovFullAt = Date.now();
  render();
}
const overview = new Feed("overview", 5000, onOverview);
const props = document.getElementById("props");
const wsec = document.getElementById("workview");
const graph = new Feed("graph", 10000, () => {
  const s = dataSig(graph);
  if (s === grSig) return;
  grSig = s;
  renderSide(side, graph.data, ui);
  nebula.update(overview.data, graph.data, healthOf(overview.data, overview).ok);
  if (sky && route() === "sky") sky.update(overview.data, graph.data, healthOf(overview.data, overview).ok);
  if (gview && route() === "graph" && graph.data) gview.update(graph.data);
});
let gview = null;
const shell = document.getElementById("shell");
const gsec = document.getElementById("graphview");
const csec = document.getElementById("chatview");
const ssec = document.getElementById("setview");
const lsec = document.getElementById("sessview");
const ksec = document.getElementById("skyview");
let sky = null;
const chat = new ChatView(csec, ui);
const nebula = initNebula(ui);     // stage 1 "Nebula" look of the board, off unless chosen
let sessTimer = null;
function route() {
  const x = location.hash;
  if (currentLook() === "nebula" && (x === "" || x === "#" || x.startsWith("#/sky"))) return "sky";   // Sky opens first
  return x.startsWith("#/graph") ? "graph" : x.startsWith("#/work/") ? "work" : x.startsWith("#/chat/") ? "chat"
    : x.startsWith("#/settings") ? "settings" : x.startsWith("#/sessions") ? "sessions" : "board";
}
let prevRoute = null;
function applyRoute() {
  const r = route();
  gsec.hidden = r !== "graph";
  wsec.hidden = r !== "work";
  csec.hidden = r !== "chat";
  ssec.hidden = r !== "settings";
  lsec.hidden = r !== "sessions";
  ksec.hidden = r !== "sky";
  shell.hidden = r !== "board";
  if (r === "sky" && !sky) sky = new Sky(ksec, ui);
  if (sky) { sky.setActive(r === "sky"); if (r === "sky") sky.update(overview.data, graph.data, healthOf(overview.data, overview).ok); }
  render();
  if (r !== "chat") chat.close();
  clearInterval(sessTimer);
  if (r === "chat") chat.open(decodeURIComponent(location.hash.slice(7)));
  if (r === "settings") renderSettings(ssec, ui);
  if (r === "sessions") { renderSessions(lsec, ui); sessTimer = setInterval(() => renderSessions(lsec, ui), 10000); }
  // Measure the field only after the new view is shown and scrolled to its top: measured first, the board's nebula
  // was placed at the previous view's scroll offset (847 px down after Graph) and looked missing.
  if (r !== prevRoute) window.scrollTo(0, 0);
  prevRoute = r;
  nebula.apply();
  if (r === "work") renderWorkItem(wsec, decodeURIComponent(location.hash.slice(7)), ui);
  const m = location.hash.match(/^#\/decision\/(.+)$/);
  if (m) setTimeout(() => focusDecision(decodeURIComponent(m[1])), 400);
  if (r === "graph") {
    if (!gview) gview = new Graph(gsec, ui);
    if (graph.data) gview.update(graph.data);
    graph.now();
    gview.canvas.focus();
    window.__graph = gview;
  }
}
window.addEventListener("hashchange", applyRoute);

// Deep link #/decision/<id>: the card is focused; on a phone it opens as a bottom sheet.
function focusDecision(id) {
  const i = ui.items.findIndex((x) => x.decision_id === id || x.id === id || x.id === "decision:" + id);
  if (i < 0) { toast("That decision is no longer open"); return; }
  ui.cur = i; ui.keyed = true; markCur(true);
  const el = document.getElementById("s-" + i);
  if (el && matchMedia("(max-width: 560px)").matches) {
    document.querySelectorAll(".strip.sheet").forEach((e) => e.classList.remove("sheet"));
    el.classList.add("sheet");
    const close = h("button", { class: "btn ghost sheet-close", onclick: () => { el.classList.remove("sheet"); close.remove(); history.replaceState(null, "", "#/"); } }, "Close");
    el.prepend(close);
  }
}

// No account connected yet (a fresh install), or none of them signed in (a sign-in was cancelled or not finished):
// the NEEDS YOU column becomes the connect-your-account screen. Unknown status counts as signed in, so a failing
// `claude auth status` never hides the board.
let firstRunShown = false, acctUsable = null;
ui.accountsSeen = (rows) => {
  const v = rows.some((r) => !r.hidden && r.signed_in !== false);
  if (v !== acctUsable) { acctUsable = v; render(); }
};
const checkAccounts = () => get("accounts").then((d) => ui.accountsSeen(d.accounts || [])).catch(() => {});
function needsOrFirstRun(ov) {
  const none = ov && (!((ov.capacity && ov.capacity.seats) || []).length || acctUsable === false);
  if (none) {
    // only while the board shows, so Settings never has a second, hidden account panel polling behind it
    if (route() !== "board") { if (firstRunShown) { firstRunShown = false; main.replaceChildren(); } return []; }
    if (!firstRunShown) { firstRunShown = true; main.replaceChildren(); renderAccounts(main, ui, { firstRun: true }); }
    return [];
  }
  if (firstRunShown) { firstRunShown = false; main.replaceChildren(); }
  return renderNeeds(main, ov, ui);
}

function render() {
  const ov = overview.data;
  renderRail(rail, ov, overview, ui);
  ui.items = needsOrFirstRun(ov);
  if (route() === "board") renderProposals(props, ov, ui);
  renderCapacity(cap, ov, ui);
  renderSide(side, graph.data, ui);
  if (ui.cur >= ui.items.length) ui.cur = Math.max(0, ui.items.length - 1);
  markCur(false);
  badge(ov);
  nebula.update(ov, graph.data, healthOf(ov, overview).ok);
  if (sky && route() === "sky") sky.update(ov, graph.data, healthOf(ov, overview).ok);
}

// ------------------------------------------------------------------ tab title + favicon
let lastBadge = "";
function badge(ov) {
  const hl = healthOf(ov, overview);
  const n = ov ? splitNeeds(ov).now.length : 0;   // parked chats (seat at a limit) are not counted
  const key = `${n}|${hl.ok}`;
  document.title = (n ? `(${n}) ` : "") + (hl.ok ? "" : "UNKNOWN ") + "Mission board";
  if (key === lastBadge) return;
  lastBadge = key;
  const c = document.createElement("canvas");
  c.width = c.height = 64;
  const g = c.getContext("2d");
  const css = getComputedStyle(document.documentElement);
  g.fillStyle = !hl.ok ? css.getPropertyValue("--warn") : n ? css.getPropertyValue("--bad") : css.getPropertyValue("--ink-3");
  g.beginPath(); g.arc(32, 32, 30, 0, Math.PI * 2); g.fill();
  g.fillStyle = "#fff";
  g.font = "bold 34px -apple-system, system-ui, sans-serif";
  g.textAlign = "center"; g.textBaseline = "middle";
  g.fillText(!hl.ok ? "?" : n > 99 ? "99" : String(n), 32, 35);
  document.getElementById("favicon").href = c.toDataURL("image/png");
}

// ------------------------------------------------------------------ since you last looked
function snapshot() {
  const ov = overview.data;
  if (!ov) return;
  store.set("lastLooked", { at: Date.now(), needIds: ov.needs_you.map((x) => x.id),
    limited: ov.needs_you.filter((x) => x.kind === "limit").map((x) => x.session_id) });
}
document.addEventListener("visibilitychange", () => {
  if (document.hidden) snapshot();
  else { ui.lastLooked = store.get("lastLooked", null); overview.now(); }
});
window.addEventListener("pagehide", snapshot);

// ------------------------------------------------------------------ theme
function applyTheme() {
  const t = store.get("theme", "system");
  if (t === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = t;
}
function cycleTheme() {
  const order = ["system", "dark", "light"];
  const t = order[(order.indexOf(store.get("theme", "system")) + 1) % 3];
  store.set("theme", t); applyTheme(); lastBadge = ""; render(); toast(`Theme: ${t}`);
}
applyTheme();

// ------------------------------------------------------------------ focus mode (server-side setting)
let pop;
async function loadFocus() {
  try { const st = await get("settings"); ui.focus = st.focus; ui.pace = st.pace; } catch { /* keep defaults; health shows failures */ }
}
function toggleFocusPanel() {
  if (pop && !pop.hidden) { pop.hidden = true; return; }
  const f = ui.focus;
  const on = h("input", { type: "checkbox", id: "f-on", checked: f.on });
  const min = h("input", { type: "number", min: "0", max: "1440", value: String(f.min_age_min), id: "f-min" });
  const win = h("input", { type: "text", value: f.windows.join(", "), id: "f-win", style: "width:12em", "aria-describedby": "f-hint" });
  const save = h("button", { class: "btn primary", onclick: async () => {
    const body = { on: on.checked, min_age_min: Number(min.value), windows: win.value.split(",").map((s) => s.trim()).filter(Boolean) };
    try { ui.focus = (await put("settings/focus", body)).focus; pop.hidden = true; toast("Focus mode saved"); render(); }
    catch (e) { toast(`Not saved: ${e.message}`); }
  } }, "Save");
  pop = pop || document.body.appendChild(h("div", { class: "pop", role: "dialog", "aria-label": "Focus mode" }));
  pop.replaceChildren(
    h("h3", {}, "Focus mode"),
    h("label", { for: "f-on" }, on, "Notify only for decisions older than"),
    h("label", { for: "f-min" }, min, "minutes"),
    h("label", { for: "f-win" }, "Check-ins", win),
    h("p", { class: "hint", id: "f-hint" }, "Local time, HH:MM, comma separated. Non-urgent items arrive in one macOS notification at each check-in. ",
      "High risk and hook holds about to expire always notify at once."),
    h("div", {}, save, " ", h("button", { class: "btn ghost", onclick: () => (pop.hidden = true) }, "Cancel")));
  pop.hidden = false;
  on.focus();
}

// ------------------------------------------------------------------ answering
// Number keys on the current strip: single question -> pick and send; several parts -> pick
// in the first unanswered part; prose with a suggested reply -> send it.
function numberKey(n) {
  const x = ui.items[ui.cur];
  if (!x || x.excluded) return;
  const parts = partsOf(x);
  if (x.kind === "decision" && parts.length) {
    const d = draftOf(ui, x.id);
    const i = parts.length === 1 ? 0 : parts.findIndex((p, j) => !(d.sel[j] && d.sel[j].length) && !(d.text[j] || "").trim());
    const p = parts[i < 0 ? parts.length - 1 : i];
    const o = p.options[n - 1];
    if (!o) return;
    d.sel[i < 0 ? parts.length - 1 : i] = [o.id];
    if (parts.length === 1 && !p.multi) { send(x, bodyFor(x, d), o.label); return; }
    render();
    return;
  }
  const btn = document.querySelectorAll(`#s-${ui.cur} .opt`)[n - 1];
  if (btn) btn.click();
}

function confirmLowRisk(list) {
  pop = pop || document.body.appendChild(h("div", { class: "pop", role: "dialog", "aria-label": "Accept low-risk recommended" }));
  pop.replaceChildren(
    h("h3", {}, `Accept ${list.length} low-risk recommended answers?`),
    h("ul", { class: "confirm-list" }, list.map((x) => h("li", {}, h("strong", {}, x.title), ": ",
      partsOf(x).map((p) => (p.options.find((o) => o.id === p.recommended) || {}).label).join(" / ")))),
    h("p", { class: "hint" }, "Each one is sent to its own chat right away."),
    h("div", {}, h("button", { class: "btn primary", onclick: () => {
      pop.hidden = true;
      list.forEach((x) => send(x, recommendedBody(x), "recommended"));
    } }, `Accept ${list.length}`), " ", h("button", { class: "btn ghost", onclick: () => (pop.hidden = true) }, "Cancel")));
  pop.hidden = false;
  pop.querySelector(".btn.primary").focus();
}

const EVIDENCE_ASK = "Before I answer: show the evidence for your last result. Name the exact command you ran to check it and paste its last output line verbatim; if nothing was verified, say so plainly.";
function askEvidence(x) {
  const d = draftOf(ui, x.id);
  d.reply = EVIDENCE_ASK;
  render();
  const ta = document.querySelector(`[data-fk="${CSS.escape(x.id + ":reply")}"]`);
  if (ta) { ta.value = EVIDENCE_ASK; ta.focus(); }
  toast("Prefilled; edit if you like, then Reply");
}

// ------------------------------------------------------------------ keyboard
function markCur(scroll) {
  document.querySelectorAll(".strip.cur").forEach((e) => e.classList.remove("cur"));
  const el = document.getElementById("s-" + ui.cur);
  if (!el || !ui.keyed) return;
  el.classList.add("cur");
  if (scroll) el.scrollIntoView({ block: "nearest", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
}
function showKeys() {
  toast("Cmd+K palette, j/k move, 1-4 answer, v review decisions, g graph, s sessions, comma settings, b briefing, f focus, t theme, r refresh, Esc close");
}
document.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); palette.open(); return; }
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (route() === "graph" && e.target === (gview && gview.canvas) && e.key !== "g") return;   // the canvas owns its keys
  if (/^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName) || e.target.isContentEditable) return;
  const k = e.key;
  if (k === "j" || k === "k") {
    if (!ui.keyed) ui.keyed = true;                      // first press selects the top item; it must not skip it
    else ui.cur = Math.max(0, Math.min(ui.items.length - 1, ui.cur + (k === "j" ? 1 : -1)));
    markCur(true); e.preventDefault();
  } else if (/^[1-9]$/.test(k) && ui.keyed) {
    numberKey(Number(k)); e.preventDefault();
  } else if (k === "t") cycleTheme();
  else if (k === "f") toggleFocusPanel();
  else if (k === "r") { overview.now(); graph.now(); toast("Refreshing"); }
  else if (k === "v" && route() === "board") ui.toggleReview();
  else if (k === "g") location.hash = route() === "graph" ? "#/" : "#/graph";
  else if (k === "s") location.hash = route() === "sessions" ? "#/" : "#/sessions";
  else if (k === ",") location.hash = route() === "settings" ? "#/" : "#/settings";
  else if (k === "?") showKeys();
  else if (k === "b") ui.voice.briefing();
  else if (k === "Escape" && pop) pop.hidden = true;
});

// ------------------------------------------------------------------ palette and voice commands
const palette = new Palette(ui);
ui.voice = new Voice(ui);
ui.palette = palette;
async function runIntent(r) {
  const ref = r.chat_ref || r.target_ref || {};
  switch (r.cmd) {
    case "board": location.hash = "#/"; return "Board.";
    case "graph": location.hash = "#/graph"; return "Graph.";
    case "needs_you": case "status": return ui.voice.summary();
    case "next": ui.keyed = true; ui.cur = Math.min(ui.items.length - 1, ui.cur + 1); markCur(true); return "Next.";
    case "skip": ui.keyed = true; ui.cur = Math.min(ui.items.length - 1, ui.cur + 1); markCur(true); return "Skipped.";
    case "answer": {
      const x = ui.items[ui.cur];
      if (!x || x.kind !== "decision") return "Move to a decision first.";
      const p = partsOf(x);
      if (p.length !== 1 || !p[0].options[r.option - 1]) return "That option is not on this card.";
      send(x, { answers: { 0: p[0].options[r.option - 1].id } }, p[0].options[r.option - 1].label);
      return "Sent.";
    }
    case "reply": {
      if (!ref.session_id) return "I could not find that chat.";
      send({ kind: "blocked", session_id: ref.session_id, key: ref.key, title: ref.title }, { text: r.text }, r.text);
      return `Reply sent to ${ref.title}.`;
    }
    case "stop": if (ref.session_id) { await ui.stopSession({ session_id: ref.session_id, label: ref.title }); return "Done."; } return "I could not find that chat.";
    case "spawn": ui.spawn({ workItem: r.work_item, seat: r.seat }); return "Opened the new session dialog.";
    case "open":
      if (ref.work_item) { location.hash = "#/work/" + encodeURIComponent(ref.work_item); return `Opened ${ref.work_item}.`; }
      if (ref.session_id) { location.hash = "#/graph"; setTimeout(() => ui.openNode("session:" + ref.key), 400); return `Opened ${ref.title}.`; }
      return "Not found.";
    default: return "Not a command I know.";
  }
}

window.__cp = ui;               // for the acceptance scripts (ui/acceptance); exposes nothing the page does not already show
if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => { /* offline shell is optional */ });

loadFocus().then(render);
applyRoute();
overview.start();
graph.start();
// Push: the server bumps a version whenever any chat changes (it re-reads every 1.5 s) and says so on /api/events.
// The board fetches at once instead of waiting up to 5 s for the next poll; polling stays as the fallback.
if (window.EventSource && TOKEN) {
  let soon = null, graphAt = 0;
  const es = new EventSource("/api/events?token=" + encodeURIComponent(TOKEN));
  es.onmessage = () => {
    clearTimeout(soon);
    soon = setTimeout(() => {
      overview.now();
      if (route() === "chat") chat.poll();         // an open chat shows its new reply at once, not on its 5 s poll
      if (Date.now() - graphAt > 3000) { graphAt = Date.now(); graph.now(); }
    }, 100);
  };
}
setInterval(render, 1000 * 15);
checkAccounts(); setInterval(() => { if (!document.hidden) checkAccounts(); }, 60000);    // ages and idle folding move even without new data
