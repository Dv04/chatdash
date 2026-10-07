// Mission board (B1): status rail, NEEDS YOU strips, work items and sessions.
import { h, icon, age, ageParts, ct, store, toast, noReading } from "./lib.js";
import { seatsAtLimit, limitWord } from "./limits.js";
import { md } from "./md.js";
import { post } from "./api.js";
import { cardBody, timeoutLine, lowRiskRecommended } from "./cards.js";
import { pace } from "./capacity.js";

const STALE_SEC = 20;      // overview older than this (or failing) turns the rail UNKNOWN
const IDLE_FOLD_SEC = 30;  // a chat that turned idle stays listed this long, then folds into "N idle"

// ------------------------------------------------------------------ health
export function healthOf(ov, feed) {
  const reasons = [];
  if (!ov) reasons.push("no data yet");
  if (feed.error) reasons.push(`last refresh failed: ${feed.error.message}`);
  if (feed.staleSec() > STALE_SEC) reasons.push(`data ${Math.round(feed.staleSec())} s old`);
  if (ov && ov.health !== "ok") reasons.push(...(ov.health_reasons || ["server reports unknown"]));
  return { ok: reasons.length === 0, reasons };
}

// ------------------------------------------------------------------ urgency order
// Hook holds about to expire first, then high risk, then oldest wait.
export function urgencyOrder(items, now = Date.now() / 1000) {
  const rank = (x) => {
    // A dialog is a live prompt on the chat's screen; in auto mode it denies itself after about a minute, so it
    // goes above everything. Ranked by age it sat at the bottom, under days-old asks .
    if (x.kind === "dialog") return 0;
    if (x.kind === "decision" && x.timeout_at && x.timeout_at - now < 120) return 0;
    if (x.risk === "high") return 1;
    return 2;
  };
  return [...items].sort((a, b) => rank(a) - rank(b) || (a.since || now) - (b.since || now));
}

// ------------------------------------------------------------------ parked: seat at a limit
// A chat whose seat is blocked (5h or 7d window full, or a limit banner; cp/sources.py seat_state) cannot act on an
// answer until the window resets, so it is parked off NEEDS YOU. The seat state clears itself at the reset.
export function splitNeeds(ov) {
  const seats = new Map(((ov && ov.capacity && ov.capacity.seats) || []).map((s) => [s.seat, s]));
  const now = [], parked = [];
  for (const x of (ov && ov.needs_you) || []) (seats.get(x.seat) || {}).state === "blocked" ? parked.push(x) : now.push(x);
  return { now, parked, seats };
}
function why(s) {
  return (s.five_hour || {}).pct >= 100 ? "5h full" : (s.seven_day || {}).pct >= 100 ? "week used up" : "at a limit";
}

// Continue a parked chat on another seat. Candidates: seats the board may start chats on (not read-only, not
// blocked or unknown, not the chat's own seat). Order: "ok" before "near" (a near seat queues the chat until it
// frees), then most spare 7-day headroom for the time left to its reset (capacity.js pace().extra).
export function continueSeats(x, seats, perDay = 14.4, now = Date.now() / 1000) {
  return [...seats.values()]
    .filter((s) => !s.excluded && s.seat !== x.seat && (s.state === "ok" || s.state === "near"))
    .map((s) => { const p = pace(s, now, perDay); return { s, spare: p.state === "unknown" ? null : p.extra, left: p.left }; })
    .sort((a, b) => (a.s.state === "ok" ? 0 : 1) - (b.s.state === "ok" ? 0 : 1)
      || (b.spare ?? -1e9) - (a.spare ?? -1e9) || (b.left ?? 0) - (a.left ?? 0));
}
export function handoffBrief(x) {
  const sid = x.session_id;
  return [
    `Continue a chat that stopped because its seat (${x.seat}) hit its usage limit. Title: ${x.title || "untitled"}${x.work_item ? `, work item ${x.work_item}` : ""}.`,
    `1. Its full transcript is a Claude Code JSONL file: ls ~/.claude*/projects/*/${sid}.jsonl (one match). Read only; never write into it or that config.`,
    `2. Work in the same directory: the "cwd" field on its lines.`,
    `3. It can be very long: read the first user message and the last 150 lines first, then only what you need.`,
    `4. Continue the work from where it stopped. First say in one line what it was doing and what you do next.`,
    x.text && x.kind !== "limit" ? `It was waiting on the user for this; ask it again here before acting on it:\n${x.text}` : "",
    x.final ? `Its last message (tail):\n${x.final}` : "",
  ].filter(Boolean).join("\n\n");
}
async function continueOn(x, seat, ui) {
  let r;
  try { r = await post("sessions", { seat, work_item: x.work_item || undefined, parent: x.session_id, brief: handoffBrief(x) }); }
  catch (e) { toast(`Not continued: ${e.message}`); return; }
  // the original must not also resume at its reset and redo the work
  const notes = [];
  store.set("continued", { ...store.get("continued", {}), [x.session_id]: x.since });
  if (!x.excluded) { try { await post("sessions/" + encodeURIComponent(x.session_id) + "/resume_pref", { pref: "off" }); } catch (e) { notes.push(`auto-resume not turned off: ${e.message}`); } }
  try { await post("needs/dismiss", { id: x.id, since: x.since, title: x.title }); } catch (e) { notes.push(`still listed: ${e.message}`); }
  toast((r.queued ? `Queued on ${seat}: starts when it frees` : `Continuing on ${seat}${r.job_id ? ` (job ${r.job_id})` : ""}`) + (notes.length ? `. ${notes.join("; ")}` : ""));
  ui.refresh();
}
let parkedNode = null;   // {sig, el}: kept across refreshes so an open list and a chosen seat survive
function parkedLine(parked, seats, ui) {
  // chats that hit the limit themselves are kept out of NEEDS YOU until the reset (cp/api.py), so they come from the seat's own stalled list
  const cont = store.get("continued", {});          // chats already continued elsewhere (per browser): they leave this list until their limit changes
  const stalled = [];
  for (const s of seats.values()) if (s.state === "blocked") for (const q of s.queued || [])
    if (!parked.some((x) => x.session_id === q.session_id) && cont[q.session_id] !== q.resets_at)
      stalled.push({ id: `limit:${s.seat}:${q.session_id}`, kind: "limit", session_id: q.session_id, seat: s.seat, title: q.name, since: q.resets_at, resets_at: q.resets_at });
  if (!parked.length && !stalled.length) { parkedNode = null; return null; }
  const perDay0 = (ui.pace && ui.pace.pct_per_day) || 14.4;
  const sig = JSON.stringify([parked.map((x) => [x.id, x.seat, x.title]), stalled.map((q) => [q.session_id, q.seat, q.resets_at]), [...seats.values()].map((s) => [s.seat, s.state, s.resume_at]),
    [...parked, ...stalled].map((x) => continueSeats(x, seats, perDay0).map((c) => [c.s.seat, Math.round(c.spare ?? -999)]))]);
  if (parkedNode && parkedNode.sig === sig) return parkedNode.el;
  const prev = parkedNode && parkedNode.el;
  const wasOpen = !!(prev && prev.open), picked = new Map(prev ? [...prev.querySelectorAll("select")].map((e) => [e.dataset.id, e.value]) : []);
  const by = new Map();
  for (const x of [...parked, ...stalled]) by.set(x.seat, (by.get(x.seat) || 0) + 1);
  const sum = [...by].map(([seat, n]) => {
    const s = seats.get(seat) || {};
    return `${s.label || seat} ${n} (${why(s)}${s.resume_at ? `, back ${ct(s.resume_at)}` : ""})`;
  });
  const perDay = (ui.pace && ui.pace.pct_per_day) || 14.4;
  const rows = [...parked, ...stalled].map((x) => {
    const cands = continueSeats(x, seats, perDay);
    const sel = h("select", { "aria-label": `Seat to continue "${x.title}" on`, disabled: !cands.length, dataset: { id: x.id } },
      cands.length ? cands.map((c, i) => h("option", { value: c.s.seat },
        `${c.s.seat}${c.spare == null ? "" : `, ${c.spare >= 0 ? "+" : ""}${Math.round(c.spare)}% spare`}${c.s.state === "near" ? ", near limit: queues" : ""}${i ? "" : " (most headroom)"}`))
        : h("option", {}, "no seat has room now"));
    if (picked.has(x.id) && cands.some((c) => c.s.seat === picked.get(x.id))) sel.value = picked.get(x.id);
    return h("li", {},
      h("span", { class: "t" }, x.title || "untitled"), h("span", { class: "chip" }, x.seat),
      h("span", { class: "grow" }),
      x.resets_at && h("span", { class: "hint" }, x.resets_at > Date.now() / 1000 ? `hit its limit, resumes ${ct(x.resets_at)}` : "hit its limit, reset passed"),
      sel, h("button", { class: "btn", disabled: !cands.length, title: "Start a new chat on that seat that reads this chat's transcript and carries on. The original stops auto-resuming and leaves this list.",
        onclick: (e) => { e.currentTarget.disabled = true; continueOn(x, sel.value, ui); } }, "Continue there"));
  });
  const el = h("details", { class: "parked", open: wasOpen },
    h("summary", {}, `${parked.length + stalled.length} more parked until their seat frees: `, sum.join(", ")),
    h("ul", {}, rows));
  parkedNode = { sig, el };
  return el;
}

// ------------------------------------------------------------------ answer ready
// Chats that finished a turn whose final answer you have not opened yet (the classic board's "Answer ready" column).
// Opening a chat marks it read; "Mark read" does the same without opening; "Mark all read" clears the list.
const snippet = (t) => (t || "").replace(/[#*`>_|]+/g, " ").replace(/\s+/g, " ").trim().slice(0, 180);
let readyNode = null;   // {sig, el}: kept across refreshes so an open list stays open
function readyLine(ov, ui) {
  const list = (ov && ov.answers_ready) || [];
  if (!list.length) { readyNode = null; return null; }
  const sig = JSON.stringify(list.map((x) => [x.id, x.final_at]));
  if (readyNode && readyNode.sig === sig) return readyNode.el;
  const wasOpen = !!(readyNode && readyNode.el.open);
  const by = new Map();
  for (const x of list) by.set(x.seat, (by.get(x.seat) || 0) + 1);
  const mark = async (fn) => { try { await fn(); } catch (e) { toast(`Not marked: ${e.message}`); } ui.refresh(); };
  const rows = list.map((x) => h("li", { class: "rd" },
    h("a", { class: "t", href: "#/chat/" + encodeURIComponent(x.session_id), title: x.title }, x.title || "untitled"),
    h("span", { class: "chip" }, x.seat), x.work_item && h("span", { class: "chip" }, x.work_item),
    h("span", { class: "grow" }),
    h("span", { class: "hint" }, `${age(x.seconds)} ago`),
    h("button", { class: "btn ghost", onclick: () => mark(() => post(`sessions/${encodeURIComponent(x.session_id)}/seen`)) }, "Mark read"),
    h("span", { class: "rd-snip" }, snippet(x.final))));
  const el = h("details", { class: "parked ready", open: wasOpen },
    h("summary", {}, `${list.length} answer${list.length === 1 ? "" : "s"} ready to read: `, [...by].map(([s, n]) => `${s} ${n}`).join(", ")),
    h("ul", {}, [h("li", {}, h("span", { class: "grow" }), h("button", { class: "btn ghost", onclick: () => mark(() => post("answers/seen_all")) }, "Mark all read")), ...rows]));
  readyNode = { sig, el };
  return el;
}

// ------------------------------------------------------------------ rail
export function renderRail(el, ov, feed, ui) {
  const hl = healthOf(ov, feed);
  const health = h("div", { class: "health " + (hl.ok ? "ok" : "unknown"), title: hl.reasons.join("\n") || "all sources read" },
    icon(hl.ok ? "ok" : "unknown"), hl.ok ? "OK" : "UNKNOWN");
  const { now: workable } = splitNeeds(ov);
  const lw = workable.reduce((m, x) => (!m || (x.seconds || 0) > (m.seconds || 0) ? x : m), null);
  const lead = h("div", { class: "lead" },
    !ov ? "Loading" :
      lw ? ["Waiting longest: ", h("b", {}, lw.title), ", ", age(lw.seconds)] :
        "Nothing waits on you");
  const c = (ov && ov.counts) || {};
  const counts = h("div", { class: "counts" },
    h("span", {}, h("b", {}, ov ? workable.length : "?"), " need you"),
    h("span", {}, h("b", {}, c.sessions_working ?? "?"), " working"),
    h("span", {}, h("b", {}, c.sessions_idle ?? "?"), " idle"),
    h("span", {}, h("b", {}, c.limited ?? "?"), " at a limit"),
    h("span", {}, h("b", {}, c.answers_ready ?? "?"), " ready"),
    !hl.ok && h("span", { class: "why" }, hl.reasons[0]));
  const seats = h("div", { class: "seats", role: "list", "aria-label": "Seat capacity" },
    ((ov && ov.capacity && ov.capacity.seats) || []).map(seatChip));
  const r = ui.route ? ui.route() : "board";
  const nav = h("nav", { class: "nav", "aria-label": "Views" },
    [["#/", "board", "Board"], ["#/sessions", "sessions", "Sessions"], ["#/graph", "graph", "Graph"], ["#/settings", "settings", "Settings"]]
      .map(([href, id, label]) => h("a", { href, class: "btn ghost", "aria-current": r === id ? "page" : false }, label)));
  const tools = h("div", { class: "rail-tools" },
    h("button", { class: "btn ghost", "aria-pressed": String(!!ui.focus.on), title: "Focus mode (f)", onclick: ui.toggleFocusPanel },
      icon("focus"), h("span", { class: "lbl" }, "Focus")),
    h("button", { class: "btn ghost", title: "Voice: briefing, hands-free, talk (b reads the briefing)", onclick: () => voiceMenu(ui), "aria-label": "Voice" }, icon("voice"), h("span", { class: "lbl" }, "Voice")),
    h("button", { class: "btn ghost", title: "Command palette (Cmd+K)", onclick: () => ui.palette.open(), "aria-label": "Command palette" }, icon("search")),
    h("button", { class: "btn ghost", title: "Theme (t)", onclick: ui.cycleTheme, "aria-label": "Switch theme" }, icon("theme")),
    h("button", { class: "btn ghost", title: "Keyboard (?)", onclick: ui.showKeys, "aria-label": "Keyboard shortcuts" }, icon("keys")));
  el.replaceChildren(health, h("div", { class: "headline" }, lead, counts), nav, tools, seats);
}

export function voiceMenu(ui) {
  const v = ui.voice;
  for (const p of document.querySelectorAll('.pop[aria-label="Focus mode"]')) p.hidden = true;   // one pop at a time
  v.panel.hidden = false;
  v.panel.replaceChildren(h("h3", {}, "Voice"),
    h("button", { class: "btn", onclick: () => v.briefing() }, "Read the briefing"),
    h("button", { class: "btn", onclick: () => v.handsFree() }, "Hands-free decisions"),
    h("button", { class: "btn", onclick: () => v.converse() }, "Talk to the dashboard"),
    h("p", { class: "hint" }, "Every command is read back and waits for your yes; sends go out at once."),
    h("button", { class: "btn ghost", onclick: () => v.stop() }, "Close"));
}

function gauge(w) {
  if (w.pct == null) {
    return h("span", { class: "gauge unk", title: w.since_reset ? "reset since last reading: unknown" : "no reading" });
  }
  const cls = w.pct >= 100 ? "full" : w.pct >= 80 ? "near" : "";
  return h("span", { class: "gauge " + cls }, h("i", { style: `width:${Math.min(100, w.pct)}%` }));
}
function pct(w) { return w.pct == null ? "?" : `${Math.round(w.pct)}%`; }

function seatChip(s) {
  const tip = [`${s.label}: ${s.state}`, `5h ${pct(s.five_hour)}${s.five_hour.resets_at ? `, resets ${ct(s.five_hour.resets_at)}` : ""}`,
    `7d ${pct(s.seven_day)}${s.seven_day.resets_at ? `, resets ${ct(s.seven_day.resets_at, true)}` : ""}`,
    s.meter_age_min != null ? `reading ${s.meter_age_min} min old` : noReading(s),
    s.excluded ? "read-only account: nothing here acts on it" : ""].filter(Boolean).join("\n");
  return h("div", { class: "seat " + s.state, role: "listitem", title: tip },
    h("span", { class: "name" }, h("span", { class: "dot", "aria-hidden": "true" }), s.seat,
      s.excluded && h("span", { class: "excluded" }, "ro"),
      h("span", { class: "sr" }, s.state)),
    gauge(s.five_hour), h("span", {}, "5h ", pct(s.five_hour)),
    gauge(s.seven_day), h("span", {}, "7d ", pct(s.seven_day)),
    s.resume_at && h("span", { style: "grid-column:1/-1" }, "resumes ", ct(s.resume_at)));
}

// ------------------------------------------------------------------ since you last looked
export function sinceLine(ov, last) {
  if (!ov || !last) return null;
  const nowIds = new Set(ov.needs_you.map((x) => x.id));
  const closed = last.needIds.filter((id) => !nowIds.has(id));
  const fresh = ov.needs_you.filter((x) => !last.needIds.includes(x.id));
  return h("div", { class: "since", "aria-label": "Since you last looked" },
    h("span", {}, "Since you last looked: "),
    h("span", {}, h("b", {}, closed.length), " closed"),
    h("span", {}, h("b", {}, fresh.length), " new"),
    h("span", { class: "when" }, ct(last.at / 1000, true)));
}

// ------------------------------------------------------------------ NEEDS YOU strips
const KIND_LABEL = { decision: "decision", dialog: "on screen", blocked: "asks you", limit: "limit" };
const nodes = new Map();      // item id -> {sig, el}: strips are rebuilt only when their content changes

function sigOf(x) {
  const { seconds, ...rest } = x;          // age moves every tick; it is updated in place
  return JSON.stringify(rest);
}

export function renderNeeds(el, ov, ui) {
  if (!ov) {
    el.replaceChildren(h("div", { class: "section-head" }, h("h2", {}, "Needs you")), h("div", { class: "skeleton" }), h("div", { class: "skeleton" }));
    return [];
  }
  const { now: workable, parked, seats } = splitNeeds(ov);
  let items = urgencyOrder(workable);
  const decisionsN = items.filter((x) => x.kind === "decision").length;
  if (ui.review) items = items.filter((x) => x.kind === "decision");
  const low = lowRiskRecommended(items);
  const head = h("div", { class: "section-head" },
    h("h2", {}, ui.review ? "Review decisions" : "Needs you"),
    h("span", { class: "meta" }, ui.review ? `${items.length} decisions` : `${items.length}, urgent first then oldest`),
    h("span", { class: "grow" }),
    decisionsN > 1 && h("button", { class: "btn ghost", "aria-pressed": String(!!ui.review), onclick: ui.toggleReview },
      ui.review ? "Show everything" : `Review ${decisionsN} decisions`),
    low.length > 1 && h("button", { class: "btn", onclick: () => ui.confirmLowRisk(low) }, `Accept ${low.length} low-risk recommended`));
  const since = ui.review ? null : sinceLine(ov, ui.lastLooked);
  if (!items.length) {
    nodes.clear();
    el.replaceChildren(head, since || "", parkedLine(parked, seats, ui) || "", readyLine(ov, ui) || "", h("div", { class: "empty" },
      h("h3", {}, ui.review ? "No open decisions" : "Nothing waits on you"),
      h("p", {}, "Questions from background chats, blocked jobs and stalled limits land here, urgent first. ",
        "Health above says whether that silence is measured.")));
    return [];
  }
  const list = el.querySelector("ol.strips") || h("ol", { class: "strips" });
  const keep = new Set();
  const els = items.map((x, i) => {
    keep.add(x.id);
    const sig = sigOf(x);
    let n = nodes.get(x.id);
    if (!n || n.sig !== sig || n.dirty) {
      n = { sig, el: strip(x, ui, () => { const m = nodes.get(x.id); if (m) m.dirty = true; ui.rerender(); }) };
      nodes.set(x.id, n);
    }
    n.el.id = "s-" + i;
    const [big, unit] = ageParts(x.seconds);
    n.el.querySelector(".age").textContent = big;
    n.el.querySelector(".unit").textContent = unit.trim() || " ";
    return n.el;
  });
  for (const id of [...nodes.keys()]) if (!keep.has(id)) nodes.delete(id);
  // Typing must never be interrupted by a refresh. Detaching a node blurs it, so the list and unchanged strips stay
  // in place: the list is reordered only when the strip order changed, and the header lines are swapped around it.
  const a = document.activeElement;
  const fk = a && a.dataset && a.dataset.fk, s0 = a && a.selectionStart, s1 = a && a.selectionEnd;
  const cur = list.children;
  if (cur.length !== els.length || els.some((e, i) => cur[i] !== e)) list.replaceChildren(...els);
  const want = [head, since, parkedLine(parked, seats, ui), readyLine(ov, ui), list].filter(Boolean);
  for (const c of [...el.childNodes]) if (!want.includes(c)) c.remove();
  want.forEach((n, i) => { if (el.childNodes[i] !== n) el.insertBefore(n, el.childNodes[i] || null); });
  if (fk && document.activeElement !== a) {
    const b = el.querySelector(`[data-fk="${CSS.escape(fk)}"]`);
    if (b) { b.focus({ preventScroll: true }); try { b.setSelectionRange(s0, s1); } catch { /* not text */ } }
  }
  return items;
}

function strip(x, ui, onChange) {
  return h("li", { class: `strip k-${x.kind}`, dataset: { id: x.id }, tabindex: "-1" },
    h("div", { class: "wait", title: x.since ? `since ${ct(x.since, true)}` : "" },
      h("span", { class: "age" }), h("span", { class: "unit" }),
      h("span", { class: "kind" }, KIND_LABEL[x.kind] || x.kind)),
    h("div", { class: "body" },
      h("div", { class: "who" },
        h("span", { class: "title" }, x.title || "untitled"),
        x.work_item && h("span", { class: "chip wi" }, x.work_item),
        h("span", { class: "chip" }, x.seat),
        x.risk && h("span", { class: "chip risk-" + x.risk, title: x.risk_why || "" }, x.risk + " risk"),
        x.mode === "dry-run" && h("span", { class: "chip ro", title: "decision hook is in dry-run" }, "dry-run"),
        x.excluded && h("span", { class: "chip ro", title: "Read-only account: shown, never acted on" }, "read-only"),
        x.running === false && h("span", { class: "chip ro", title: "The chat's process is not running. The question was never answered, so it stays; answering it here resumes the chat in the background." }, "chat stopped: answering resumes it"),
        h("span", { class: "grow" }),
        h("button", { class: "btn ghost dismiss", title: "Remove this from NEEDS YOU without answering (logged). A new question from the chat shows again.",
          onclick: async () => {
            try { await post("needs/dismiss", { id: x.id, since: x.since, title: x.title }); toast("Dismissed"); ui.refresh(); }
            catch (e) { toast(`Not dismissed: ${e.message}`); } } }, "Dismiss")),
      cardBody(x, ui, onChange),
      h("div", { class: "timeout" }, timeoutLine(x)),
      h("details", { class: "evidence", open: true },
        h("summary", {}, "Last message", x.final_at ? `, ${ct(Date.parse(x.final_at) / 1000, true)}` : ""),
        x.session_id && h("a", { class: "btn ghost history", href: "#/chat/" + encodeURIComponent(x.session_id) }, "Full chat history"),
        x.last_prompt && h("p", { class: "final" }, "You: ", x.last_prompt),
        x.final ? md(x.final, "final md") : h("p", { class: "final" }, "(no final message read)"),
        receiptView(x.receipt, x, ui))));
}

export function receiptView(r, x, ui) {
  const gate = !x.excluded && x.kind !== "decision" && h("button", { class: "btn ghost gate", "aria-pressed": String(!!x.gate_on),
    title: "Evidence gate: block this chat from ending a turn that changed files without a named check (max 3 times)",
    onclick: () => ui.setGate(x, !x.gate_on) }, x.gate_on ? "Gate on" : "Gate off");
  if (!r) {
    return h("div", { class: "receipt none" }, "No receipt for the last turn",
      !x.excluded && x.kind !== "decision" && h("button", { class: "btn ghost", onclick: () => ui.askEvidence(x) }, "Ask for evidence"), gate);
  }
  return h("div", { class: "receipt-wrap" }, receiptChips(r),
    h("div", { class: "receipt-tools" },
      !r.verified && !x.excluded && x.kind !== "decision" && h("button", { class: "btn ghost", onclick: () => ui.askEvidence(x) }, "Ask for evidence"),
      gate));
}

export function receiptChips(r) {
  const d = r.diff || {};
  const t = r.tests || null;
  const files = r.files || [];
  return h("div", { class: "receipt" + (r.verified ? "" : " unverified") },
    h("span", { class: "chip " + (r.verified ? "risk-low" : "risk-med"), title: r.verified ? "evidence rule passed" : "no named check, no honest not-verified label" },
      r.verified ? "verified" : "not verified"),
    files.length ? h("details", { class: "files" },
      h("summary", { class: "chip", title: d.source || "" }, `${d.files ?? files.length} files`, d.add != null ? ` +${d.add} -${d.del}` : ""),
      h("ul", {}, files.slice(0, 40).map((f) => h("li", {}, h("code", {}, f.path), " ", h("span", { class: "hint" }, f.source)))))
      : h("span", { class: "chip" }, "0 files"),
    t && t.last_line ? h("span", { class: "testline", title: t.cmd || "" }, t.last_line) : h("span", { class: "chip" }, "no test line"),
    (r.prs || []).map((u) => h("a", { href: u, target: "_blank", rel: "noopener noreferrer" }, u.replace(/^https:\/\/github.com\//, ""))),
    (d.commits || []).length > 0 && h("span", { class: "chip", title: d.commits.join("\n") }, `${d.commits.length} commit${d.commits.length === 1 ? "" : "s"}`),
    r.cost_units != null && h("span", { class: "chip", title: "limit units this turn" }, `${Math.round(r.cost_units / 1000)}k units`),
    h("span", { class: "hint" }, `turn ${r.turn}, ${ct(r.at, true)}`));
}

// ------------------------------------------------------------------ side: work items / seats
const idleSeen = new Map();   // session id -> first time seen idle (ms)

export function renderSide(el, graph, ui, opts = {}) {
  const mode = ui.group, MODES = ["work item", "seat", "recent"];
  const seg = h("div", { class: "seg", role: "group", "aria-label": "Group by" },
    MODES.map((m, i) => h("button", { "aria-pressed": String(mode === m), title: `Group by ${m} (key ${i + 1} while the list has focus)`, onclick: () => ui.setGroup(m) }, m)));
  const head = h("div", { class: "section-head" }, h("h2", {}, "Sessions"), h("span", { class: "grow" }),
    h("button", { class: "btn primary", title: "Start a new chat (pick the seat, optionally as a workflow)", onclick: () => ui.spawn({}) }, "+ New chat"), seg);
  if (!graph) { el.replaceChildren(head, h("div", { class: "skeleton" })); return; }
  // A filter that survives the 1.5 s refresh: the input node is kept, only the lists around it are rebuilt.
  const filt = el._filt || (el._filt = h("input", { type: "search", class: "free side-filter", placeholder: "Filter chats (press / in the list)",
    "aria-label": "Filter chats", oninput: () => el._graph && renderSide(el, el._graph, ui, el._opts) }));
  const hadFocus = el.contains(document.activeElement) && document.activeElement.dataset ? document.activeElement.dataset.sid : null;
  el._graph = graph; el._opts = opts;
  if (!el._kb) { el._kb = true; el.addEventListener("keydown", (e) => listKeys(e, el, ui)); }
  const q = filt.value.trim().toLowerCase();
  const all = graph.nodes.filter((n) => n.type === "session");
  const sessions = q ? all.filter((n) => `${n.label} ${n.seat} ${n.parent || ""} ${labelOf(graph, n.parent)}`.toLowerCase().includes(q)) : all;
  const labels = Object.fromEntries(graph.nodes.filter((n) => n.type === "work_item").map((n) => [n.id, n.label]));
  const ready = new Set((((ui.overview && ui.overview()) || {}).answers_ready || []).map((x) => x.session_id));
  const sidOf = (s) => s.session_id || s.id.replace(/^session:[^:]+:/, "");
  const atLimit = seatsAtLimit(null, graph);
  const groups = new Map();
  if (mode === "recent") groups.set("recent", [...sessions].sort((a, b) => (b.activity || 0) - (a.activity || 0)).slice(0, 40));
  else for (const s of sessions) {
    const key = mode === "seat" ? s.seat : (s.parent || "none");
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(s);
  }
  const now = Date.now();
  for (const s of sessions) {
    if (s.state === "idle") { if (!idleSeen.has(s.id)) idleSeen.set(s.id, now); } else idleSeen.delete(s.id);
  }
  const order = [...groups.entries()].sort((a, b) => mode === "recent" ? 0 : score(b[1]) - score(a[1]) || String(a[0]).localeCompare(String(b[0])));
  const blocks = order.map(([key, list]) => {
    const title = mode === "recent" ? "Most recent" : mode === "seat" ? key : key === "none" ? "No work item" : (labels[key] || key).replace(/^([A-Za-z][A-Za-z0-9]*-\d+)\s.*/, "$1") + " " +
      ((labels[key] || "").replace(/^[A-Za-z][A-Za-z0-9]*-\d+\s*/, "").split(" - ")[0]);
    const shown = mode === "recent" ? list : list.filter((s) => s.state !== "idle" || now - idleSeen.get(s.id) < IDLE_FOLD_SEC * 1000 || ui.expanded.has(key) || ready.has(sidOf(s)) || sidOf(s) === opts.cur);
    const folded = list.length - shown.length;
    return h("section", { class: "group" },
      h("h3", {}, title, h("span", { class: "n" }, `${list.length}`)),
      h("ul", { class: "sess" }, (mode === "recent" ? shown : shown.sort(byState)).map((s) => {
        const sid = sidOf(s), isReady = s.state === "idle" && !s.limited && ready.has(sid);
        const lw = limitWord(s, atLimit);   // same rule as the header (splitNeeds parks those items), whatever the chat's own state says
        const cls = lw ? "limited" : s.needs_you ? "needs_you" : isReady ? "ready" : s.state;
        const word = lw || (s.needs_you ? "needs you" : isReady ? "answer ready" : s.state);
        return h("li", { title: s.final || "" },
          h("span", { class: "st " + cls, "aria-hidden": "true", title: word }),
          h("a", { class: "nm", href: "#/chat/" + encodeURIComponent(sid), dataset: { sid }, "aria-current": sid === opts.cur ? "true" : "false" }, s.label, h("span", { class: "sr" }, ", ", word)),
          h("span", { class: "rt" }, mode === "recent" ? `${s.seat}${s.activity ? ", " + age(Date.now() / 1000 - s.activity) : ""}` : mode === "seat" ? (s.parent || "").replace("work_item:", "") : s.seat));
      })),
      folded > 0 && h("button", { class: "idle-fold", onclick: () => ui.expand(key) }, `${folded} idle`));
  });
  // Chats with no process and no job: not on the graph, but never invisible. Folded under their count; one click lists them.
  const closed = (graph.closed || []).filter((c) => !q || `${c.label} ${c.seat}`.toLowerCase().includes(q));
  if (closed.length) {
    const open = ui.expanded.has("closed") || (q && closed.length);
    const total = Math.max(graph.closed_total || 0, closed.length);
    blocks.push(h("section", { class: "group" },
      h("h3", {}, "Closed", h("span", { class: "n" }, `${total}`)),
      open ? h("ul", { class: "sess" }, closed.map((c) => h("li", {},
        h("span", { class: "st stopped", "aria-hidden": "true", title: "closed" }),
        h("a", { class: "nm", href: "#/chat/" + encodeURIComponent(c.session_id), dataset: { sid: c.session_id },
          "aria-current": c.session_id === opts.cur ? "true" : "false" }, c.label, h("span", { class: "sr" }, ", closed")),
        h("span", { class: "rt" }, `${c.seat}${c.activity ? ", " + age(Date.now() / 1000 - c.activity) : ""}`))))
        : h("button", { class: "idle-fold", onclick: () => ui.expand("closed") }, `${total} closed, show`)));
  }
  if (q && !blocks.length) blocks.push(h("p", { class: "hint" }, `No chat matches "${filt.value.trim()}".`));
  if (filt.parentNode !== el) el.replaceChildren(head, filt, ...blocks);
  else { for (const n of [...el.childNodes]) if (n !== filt) n.remove(); el.insertBefore(head, filt); el.append(...blocks); }
  if (hadFocus) { const n = el.querySelector(`a.nm[data-sid="${CSS.escape(hadFocus)}"]`); if (n) n.focus({ preventScroll: true }); }   // the 1.5 s refresh rebuilds the rows: the keyboard must not lose its place
}

// Keyboard for the Sessions list (the board window and the split view): arrows or j/k move, Enter opens and focuses the reply box,
// 1/2/3 pick the grouping, / filters, Esc leaves. In the split view the open chat follows the highlighted row.
function listKeys(e, el, ui) {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const t = e.target, split = !!(el._opts && el._opts.split);
  const rows = () => [...el.querySelectorAll("a.nm")];
  const stop = () => { e.preventDefault(); e.stopPropagation(); };
  const go = (d) => {
    const r = rows(); if (!r.length) return;
    const i = r.indexOf(t), n = r[i < 0 ? (d > 0 ? 0 : r.length - 1) : Math.max(0, Math.min(r.length - 1, i + d))];
    n.focus(); n.scrollIntoView({ block: "nearest" });
    if (split) ui.previewChat(n.dataset.sid);
  };
  if (t.matches && t.matches("input.side-filter")) {
    if (e.key === "ArrowDown") { stop(); go(1); }
    else if (e.key === "Escape") { e.stopPropagation(); t.value = ""; t.blur(); renderSide(el, el._graph, ui, el._opts); }
    return;                                              // typing in the box is untouched
  }
  if (!t.matches || !t.matches("a.nm")) return;
  const k = e.key;
  if (k === "ArrowDown" || k === "j") { stop(); go(1); }
  else if (k === "ArrowUp" || k === "k") { stop(); go(-1); }
  else if (k === "Enter" || k === "ArrowRight") { stop(); ui.openChat(t.dataset.sid, { focus: true }); }       // into the reply box
  else if (/^[123]$/.test(k)) { stop(); ui.setGroup(["work item", "seat", "recent"][Number(k) - 1]); setTimeout(() => ui.focusList(), 0); }
  else if (k === "/" && el._filt) { stop(); el._filt.focus(); }
  else if (k === "Escape") { e.stopPropagation(); if (split) location.hash = "#/"; else t.blur(); }
}
function labelOf(graph, id) { const n = id && graph.nodes.find((x) => x.id === id); return n ? n.label : ""; }

const RANK = { needs_you: 0, working: 1, idle: 2, stopped: 3 };
function byState(a, b) { return (RANK[a.state] ?? 4) - (RANK[b.state] ?? 4) || (b.activity || 0) - (a.activity || 0); }
function score(list) { return list.reduce((t, s) => t + (s.needs_you ? 100 : 0) + (s.state === "working" ? 10 : 0), 0); }
