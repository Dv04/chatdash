// Chat history (#/chat/<session id>): the whole transcript, thinking and tool calls included, with the
// controls for that chat (reply, keep warm, stop, terminal). Live chats refresh in place every 5 s;
// a refresh never steals the scroll position or the reply being typed.
import { h, ct, age, toast } from "./lib.js";
import { seatsAtLimit, limitWord } from "./limits.js";
import { md } from "./md.js";
import { get, post, TOKEN } from "./api.js";
import { micFor } from "./dictate.js";
import { cardBody, timeoutLine } from "./cards.js";

const PAGE = 250;
const SHOW = { thinking: true, tools: true };

export class ChatView {
  constructor(el, ui, opts = {}) {
    this.el = el; this.ui = ui; this.embedded = !!opts.embedded; this.sid = null; this.entries = []; this.start = 0; this.total = 0;   // embedded: the split view, where the chat scrolls inside its pane
    this.timer = null; this.filter = ""; this.drafts = new Map(); this.attachBy = new Map();   // files attached to the reply in progress, per chat
     this.openSet = new Set();    // entries you expanded ("sid:index"): they stay open when the log is rebuilt      // a half-typed reply survives switching to another chat and back
    this.show = { ...SHOW, ...(ui.store.get("chatShow", {})) };
    // "N new" jump button: shown when entries arrive while you are reading further up; gone once you reach the end.
    this.jump = h("button", { class: "btn jump-new", hidden: true, onclick: () => this.toEnd() });
    window.addEventListener("scroll", () => { if (!this.jump.hidden && this.nearEnd()) this.jump.hidden = true; }, { passive: true });
  }
  sc() { return this.embedded && this.scEl && this.el.contains(this.scEl) ? this.scEl : null; }
  scrollTop() { const s = this.sc(); return s ? s.scrollTop : window.scrollY; }
  scrollH() { const s = this.sc(); return s ? s.scrollHeight : document.documentElement.scrollHeight; }
  viewH() { const s = this.sc(); return s ? s.clientHeight : window.innerHeight; }
  scrollTo(top, smooth) {
    const behavior = smooth && !matchMedia("(prefers-reduced-motion: reduce)").matches ? "smooth" : "auto";
    (this.sc() || window).scrollTo({ top, behavior });
  }
  nearEnd() { return this.scrollH() - this.scrollTop() - this.viewH() < 160; }
  toEnd() { this.jump.hidden = true; this.scrollTo(this.scrollH(), true); }
  focusBox() { const ta = this.box && this.box.querySelector("textarea"); if (ta) ta.focus({ preventScroll: true }); }

  async open(sid) {
    clearInterval(this.timer);
    if (sid !== this.sid) { if (this.sid) this.drafts.set(this.sid, this.reply || ""); this.sid = sid; this.entries = []; this.reply = this.drafts.get(sid) || ""; }
    const tok = this.tok = (this.tok || 0) + 1;       // arrowing through chats fast: only the last one opened may paint
    this.built = null;
    if (this.embedded && this.pageEl && this.el.contains(this.pageEl)) this.el.classList.add("loading");   // keep the old chat dimmed instead of a flash
    else this.el.replaceChildren(h("div", { class: "skeleton" }));
    try {
      const d = await get(`sessions/${encodeURIComponent(sid)}/transcript?limit=${PAGE}`);
      if (tok !== this.tok) return;
      this.session = d.session; this.entries = d.entries; this.start = d.start; this.total = d.total; this.counts = d.counts;
      post(`sessions/${encodeURIComponent(sid)}/seen`).catch(() => {});      // opening a chat reads its answer (it leaves "answers ready")
    } catch (e) {
      if (tok !== this.tok) return;
      this.el.classList.remove("loading");
      this.el.replaceChildren(h("div", { class: "page" }, h("a", { href: "#/sessions", class: "hint" }, "Sessions"),
        h("div", { class: "empty" }, h("h3", {}, "Chat not found"), h("p", {}, e.message))));
      return;
    }
    this.el.classList.remove("loading");
    this.render(true);
    if (this.wantFocus) { this.wantFocus = false; this.focusBox(); }
    this.timer = setInterval(() => this.poll(), 5000);
  }

  close() { clearInterval(this.timer); this.timer = null; }

  async poll() {
    if (document.hidden || !this.sid) return;
    try {
      const d = await get(`sessions/${encodeURIComponent(this.sid)}/transcript?limit=${PAGE}`);
      const changed = d.total !== this.total || JSON.stringify(d.session) !== JSON.stringify(this.session) ||
        (d.entries.length && JSON.stringify(d.entries[d.entries.length - 1]) !== JSON.stringify(this.entries[this.entries.length - 1]));
      if (!changed) return;
      const atBottom = this.nearEnd() && !this.log.querySelector("details[open]"), fresh = Math.max(0, d.total - this.total);
      // keep older pages already loaded; replace the tail from the server
      const keep = this.entries.filter((e) => e.i < d.start);
      this.entries = keep.concat(d.entries);
      this.start = keep.length ? this.start : d.start;
      this.total = d.total; this.session = d.session; this.counts = d.counts;
      this.render(atBottom);
      if (!atBottom && fresh) {
        this.unseen = (this.jump.hidden ? 0 : this.unseen || 0) + fresh;
        this.jump.textContent = `${this.unseen} new \u2193`; this.jump.hidden = false;
      }
    } catch { /* the rail shows feed health; a missed poll is retried */ }
  }

  async older() {
    try {
      const d = await get(`sessions/${encodeURIComponent(this.sid)}/transcript?limit=${PAGE}&before=${this.start}`);
      const prevH = this.scrollH(), prevY = this.scrollTop();
      this.entries = d.entries.concat(this.entries); this.start = d.start;
      this.render(false);
      this.scrollTo(prevY + this.scrollH() - prevH);         // stay on the entry you were reading
    } catch (e) { toast(`Could not load: ${e.message}`); }
  }

  render(toBottom) {
    const s = this.session;
    const ro = s.excluded;
    const kw = s.kw;
    const act = async (path, body, ok) => {
      try { const r = await post(`sessions/${encodeURIComponent(s.session_id)}/${path}`, body); toast(r && r.error ? r.error : ok); this.poll(); }
      catch (e) { toast(`Failed: ${e.message}`); }
    };
    const toggles = h("div", { class: "seg", role: "group", "aria-label": "Show" },
      ["thinking", "tools"].map((k) => h("button", { "aria-pressed": String(this.show[k]), onclick: () => {
        this.show[k] = !this.show[k]; this.ui.store.set("chatShow", this.show); this.render(false);
      } }, k)));
    const find = h("input", { type: "search", class: "field", placeholder: "Find in loaded history", value: this.filter, "aria-label": "Find in history",
      oninput: (e) => { this.filter = e.target.value; this.paint(); } });
    const head = h("div", { class: "chat-head" },
      h("div", { class: "chat-title" },
        h("a", { href: "#/sessions", class: "hint" }, "Sessions"),
        h("h1", {}, s.name),
        h("div", { class: "who" },
          (() => { const lw = limitWord(s, seatsAtLimit(this.ui.overview && this.ui.overview(), this.ui.graphData && this.ui.graphData()));
            return h("span", { class: "chip " + (lw ? "st-near" : "st-" + s.state) }, lw || (s.live ? s.state : "not running")); })(),
          h("span", { class: "chip" }, s.seat),
          s.work_item && h("a", { class: "chip wi", href: "#/work/" + encodeURIComponent(s.work_item) }, s.work_item),
          s.cache_age_min != null && h("span", { class: "chip", title: "prompt cache lives 1 h from the last call" },
            `cache ${s.warmth || "?"}, last call ${Math.round(s.cache_age_min)} min ago`),
          kw && kw.on && h("span", { class: "chip risk-low", title: kw.detail || "" }, `keep-warm on, ${kw.pings} pings, stops in ${kw.stop_in_h} h`),
          ro && h("span", { class: "chip ro" }, "read-only seat"),
          h("span", { class: "hint" }, `${this.total} entries: ${fmtCounts(this.counts)}`),
          this.embedded && h("button", { class: "btn ghost tools-toggle", title: "Thinking and tool toggles, find, keep-warm, limit resume, terminal, stop",
            onclick: () => this.pageEl.classList.toggle("tools-open") }, "Tools"))),
      h("div", { class: "chat-tools" }, toggles, find,
        !ro && h("button", { class: "btn", title: "One tiny ping that renews the 1 h prompt cache", onclick: () => act("keepwarm", { action: "now" }, "Keep-warm ping sent") }, "Keep warm now"),
        !ro && h("button", { class: "btn", "aria-pressed": String(!!(kw && kw.on)), title: "Ping just before the cache expires, for 12 h",
          onclick: () => act("keepwarm", { action: kw && kw.on ? "off" : "on", hours: 12 }, kw && kw.on ? "Auto keep-warm off" : "Auto keep-warm on for 12 h") },
          kw && kw.on ? "Auto keep-warm: on" : "Auto keep-warm: off"),
        resumeSeg(s, async (pref) => {
          try { const r = await post(`sessions/${encodeURIComponent(s.session_id)}/resume_pref`, { pref }); this.session = r.session; this.render(false);
            toast(pref === "on" ? "Limit resume ON for this chat: it is resumed when its limit resets" : pref === "off" ? "Limit resume off for this chat" : `This chat follows the global setting (${s.resume.global})`); }
          catch (e) { toast(`Not changed: ${e.message}`); }
        }),
        !ro && h("button", { class: "btn ghost", onclick: () => this.ui.terminal(s) }, "Open in Terminal"),
        !ro && s.live && h("button", { class: "btn ghost", onclick: () => this.ui.stopSession({ session_id: s.session_id, label: s.name }) }, "Stop")));
    this.log = h("ol", { class: "chat-log", "aria-label": "Chat history" });
    // Remember what you open and close; paint() re-opens it after every rebuild (a new tool call or thinking block used to close them all).
    this.log.addEventListener("toggle", (ev) => {
      const li = ev.target.closest && ev.target.closest("li.e");
      if (!li || li.dataset.i == null) return;
      const k = `${this.sid}:${li.dataset.i}`;
      if (ev.target.open) this.openSet.add(k); else this.openSet.delete(k);
    }, true);
    const more = this.start > 0 && h("button", { class: "btn ghost older", onclick: () => this.older() }, `Load ${Math.min(PAGE, this.start)} earlier (${this.start} not loaded)`);
    const y = this.scrollTop();
    // The page skeleton and the reply box are built once per chat; a refresh swaps the header and the log only, and
    // leaves the header alone while you type in its find box, so typing is never interrupted.
    const key = `${this.sid}:${ro}`;
    if (this.built !== key || !this.el.contains(this.pageEl)) {
      this.built = key;
      this.headSlot = h("div", { class: "chat-head-slot" });
      this.moreSlot = h("div", {});
      this.logSlot = h("div", {});
      this.box = ro ? h("div", { class: "chat-reply ro hint" }, "Read-only: this account is marked read-only in your DHI Orbit config, so this board does not reply, stop or open it. Use that account's own terminal.")
        : h("div", { class: "chat-reply" },
          this.attachStrip = h("div", { class: "attach-strip", hidden: true, "aria-live": "polite" }),
          h("textarea", { class: "reply", rows: "3", "aria-label": "Reply", oninput: (e) => (this.reply = e.target.value),
            onpaste: (e) => { const fs = e.clipboardData && e.clipboardData.files; if (fs && fs.length) { e.preventDefault(); this.attach(fs); } },   // a pasted screenshot or file
            onkeydown: (e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); this.send(); }
              else if (this.embedded && this.ui.focusList && (e.key === "Escape" || (e.key === "ArrowLeft" && (e.target.value === "" || e.altKey)))) {
                e.preventDefault(); this.ui.focusList();       // back to the list: arrows change chat
              }
            } }),
          h("button", { class: "btn primary", onclick: () => this.send() }, "Send"));
      const ta = this.box.querySelector("textarea");
      if (ta) {
        const pick = h("input", { type: "file", multiple: true, hidden: true, accept: ATTACH_ACCEPT, onchange: (e) => { this.attach(e.target.files); e.target.value = ""; } });
        ta.after(h("button", { type: "button", class: "btn ghost attach-btn", title: "Attach images, PDFs or documents (or paste or drop them here)", onclick: () => pick.click() }, "Attach"), pick);
        ta.after(micFor(ta));
      }
      if (ta) ta.value = this.reply || "";
      this.jump.hidden = true;
      this.scEl = h("div", { class: "chat-scroll" }, this.moreSlot, this.logSlot);
      if (this.embedded) this.scEl.addEventListener("scroll", () => { if (!this.jump.hidden && this.nearEnd()) this.jump.hidden = true; }, { passive: true });
      this.needSlot = h("div", { class: "chat-need", hidden: true }); this.needSig = null;
      this.pageEl = h("div", { class: "page chat" }, this.headSlot, this.scEl, this.jump, this.needSlot, this.box);
      this.el.replaceChildren(this.pageEl);
      if (!ro) {   // drop files anywhere on the chat
        const files = (e) => e.dataTransfer && [...(e.dataTransfer.types || [])].includes("Files");
        this.pageEl.addEventListener("dragover", (e) => { if (files(e)) { e.preventDefault(); this.pageEl.classList.add("drop"); } });
        this.pageEl.addEventListener("dragleave", (e) => { if (!this.pageEl.contains(e.relatedTarget)) this.pageEl.classList.remove("drop"); });
        this.pageEl.addEventListener("drop", (e) => { if (files(e)) { e.preventDefault(); this.pageEl.classList.remove("drop"); this.attach(e.dataTransfer.files); } });
      }
    }
    this.paintAttach();
    const ta = this.box.querySelector("textarea");
    if (ta) ta.placeholder = s.live ? (this.embedded ? "Reply (Cmd+Enter sends). While this box is empty: Up and Down switch chat, Left goes to the list" : "Reply to this chat (Cmd+Enter sends)") : "This chat is not running; a reply resumes it in the background";
    if (!this.headSlot.contains(document.activeElement) || !this.headSlot.firstChild) this.headSlot.replaceChildren(head);
    this.moreSlot.replaceChildren(more || "");
    this.logSlot.replaceChildren(this.log,
      s.live && s.state === "working" ? h("p", { class: "chat-working", role: "status" }, h("i", {}), h("i", {}), h("i", {}), " Claude is working") : "");
    this.paint();
    requestAnimationFrame(() => this.scrollTo(toBottom ? this.scrollH() : y));
    this.renderNeed();
  }

  // The question this chat is waiting on (a permission dialog, a decision, a blocked job), answered right here with the board's own card:
  // the same options and numbers as the terminal. Rebuilt only when the item changes, so a half-typed answer survives the refreshes.
  renderNeed() {
    if (!this.embedded || !this.needSlot) return;
    const x = this.ui.itemFor(this.sid);
    if (!x || x.excluded) { this.setBlocked(false); this.needSlot.hidden = true; this.needSlot.replaceChildren(); this.needSig = null; return; }
    // A dialog on the chat's screen takes the keys: typing a reply into the terminal now would answer it with the wrong characters. The box
    // stays locked until the dialog is answered or dismissed.
    const dialog = x.kind === "dialog" || !!(x.screen && (x.screen.options || []).length);
    this.setBlocked(dialog);
    const waiting = dialog ? this.entries.filter((e) => e.kind === "tool" && e.result == null).slice(-2) : [];   // the call that is asking
    const sig = JSON.stringify([x.id, x.since, x.delivery, x.text, x.screen && x.screen.options, x.parts && x.parts.length, waiting.map((e) => e.id)]);
    if (sig === this.needSig && this.needSlot.firstChild) return;
    this.needSig = sig; this.needSlot.hidden = false;
    const KIND = { dialog: "Waiting for your approval", decision: "A question for you", blocked: "Waiting on you", limit: "Stopped at a limit" };
    this.needSlot.replaceChildren(
      h("div", { class: "chat-need-head" }, h("b", {}, KIND[x.kind] || "Needs you"),
        h("span", { class: "hint" }, "Press 1, 2, 3 to answer, or Option+1, 2, 3 from anywhere"), h("span", { class: "grow" }),
        h("button", { class: "btn ghost", title: "Remove this from NEEDS YOU without answering (logged)", onclick: async () => {
          try { await post("needs/dismiss", { id: x.id, since: x.since, title: x.title }); toast("Dismissed"); this.ui.refresh(); } catch (e) { toast(`Not dismissed: ${e.message}`); } } }, "Dismiss")),
      waiting.length > 0 && h("div", { class: "need-tool" }, waiting.map((e) => toolAsk(e))),
      cardBody(x, this.ui, () => { this.needSig = null; this.renderNeed(); }),
      h("div", { class: "timeout" }, timeoutLine(x)));
  }

  setBlocked(b) {
    this.blocked = b;
    if (!this.box) return;
    this.box.inert = b;                                // unfocusable and untypable, mic and Attach included
    this.box.classList.toggle("locked", b);
    const ta = this.box.querySelector("textarea");
    if (b && ta) ta.placeholder = "A question is waiting above. Answer it with 1, 2, 3 (or Dismiss it), then reply.";
  }

  paint() {
    const f = this.filter.trim().toLowerCase();
    const rows = this.entries.filter((e) => (this.show.thinking || e.kind !== "thinking") && (this.show.tools || e.kind !== "tool") &&
      (!f || [e.text, e.summary, e.input, e.result, e.name].some((v) => v && v.toLowerCase().includes(f))));
    this.log.replaceChildren(...(rows.length ? rows.map((e) => {
      const li = entry(e);
      li.dataset.i = e.i;
      const d = li.querySelector("details");
      if (d && this.openSet.has(`${this.sid}:${e.i}`)) d.open = true;
      return li;
    }) : [h("li", { class: "hint" }, f ? "Nothing matches in the loaded history" : "No entries")]));
  }

  get pend() { let a = this.attachBy.get(this.sid); if (!a) this.attachBy.set(this.sid, a = []); return a; }

  paintAttach() {
    if (!this.attachStrip) return;
    const a = this.pend;
    this.attachStrip.hidden = !a.length;
    this.attachStrip.replaceChildren(...a.map((f) => h("span", { class: "attach-chip" + (f.err ? " err" : "") },
      h("span", { class: "nm", title: f.path || f.err || "" }, f.name),
      h("span", { class: "hint" }, f.err || (f.path ? fmtSize(f.size) : "uploading")),
      h("button", { type: "button", class: "x", "aria-label": `Remove ${f.name}`, onclick: () => { const i = a.indexOf(f); if (i >= 0) a.splice(i, 1); this.paintAttach(); } }, "\u00d7"))));
  }

  // Each file is POSTed as raw bytes; the server saves it and returns its absolute path, which send() puts in the reply for the agent to read.
  async attach(list) {
    if (this.blocked) { toast("Answer the question above first"); return; }
    const a = this.pend, sid = this.sid;
    for (const file of [...list]) {
      const f = { name: file.name || `pasted-${Date.now()}.png`, size: file.size };
      if (file.size > ATTACH_MAX) { toast(`${f.name} is over ${ATTACH_MAX / 1048576} MB`); continue; }
      a.push(f); this.paintAttach();
      try {
        const res = await fetch("/api/cp/uploads", { method: "POST", cache: "no-store", body: file,
          headers: { "X-Token": TOKEN, "X-Filename": encodeURIComponent(f.name), "Content-Type": file.type || "application/octet-stream" } });
        const d = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(d.error || `HTTP ${res.status}`);
        Object.assign(f, { path: d.path, size: d.size, image: d.image });
      } catch (e) { f.err = e.message; toast(`Not attached: ${e.message}`); }
      if (this.sid === sid) this.paintAttach();
    }
  }

  async send() {
    if (this.blocked) { toast("Answer the question above first"); return; }
    const a = this.pend;
    if (a.some((f) => !f.path && !f.err)) { toast("Still uploading"); return; }
    const files = a.filter((f) => f.path);
    let text = (this.reply || "").trim();
    if (!text && !files.length) return;
    if (files.length) text += (text ? "\n\n" : "") + `Attached ${files.length === 1 ? "file" : "files"} (read ${files.length === 1 ? "it" : "them"} with your file tools):\n` + files.map((f) => f.path).join("\n");
    try {
      const r = await post(`sessions/${encodeURIComponent(this.session.session_id)}/reply`, { text, key: this.session.key });
      toast(r.queued ? "Queued: it is typed in when the chat finishes its turn" : "Sent");
      a.length = 0; this.paintAttach();
      this.reply = ""; const ta = this.el.querySelector(".chat-reply textarea"); if (ta) ta.value = "";
      setTimeout(() => this.poll(), 600);
    } catch (e) { toast(`Not sent: ${e.message}`); }
  }
}

// Per-chat limit resume: default follows the global mode; on resumes this chat even while the global
// mode is dry-run or off; off never resumes it.
export function resumeSeg(s, onPick) {
  const r = s.resume || {};
  if (!r.available) return h("span", { class: "chip ro", title: r.why || "" }, "limit resume: n/a");
  const cur = r.pref || "default";
  return h("div", { class: "seg", role: "radiogroup", "aria-label": "Limit resume for this chat",
    title: `Limit resume for this chat. default = the global setting (now ${r.global}). Effective: ${r.effective}.` },
    h("span", { class: "seg-lbl" }, "Limit resume"),
    [["default", `default (${r.global})`], ["on", "on"], ["off", "off"]].map(([v, l]) =>
      h("button", { role: "radio", "aria-checked": String(cur === v), "aria-pressed": String(cur === v), onclick: () => cur !== v && onPick(v) }, l)));
}

// What the waiting call is: its name and the fields of its input (command, path, description ...), so an approval says what it approves.
function toolAsk(e) {
  let inp = null;
  try { inp = JSON.parse(e.input); } catch { /* cut or not JSON: shown as text */ }
  const rows = inp && typeof inp === "object" ? Object.entries(inp).filter(([, v]) => typeof v === "string" || typeof v === "number" || typeof v === "boolean") : [];
  return h("div", { class: "need-call" }, h("b", {}, e.name || "Tool"),
    rows.length ? h("dl", {}, rows.slice(0, 6).flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, String(v).slice(0, 700))]))
      : h("pre", {}, (e.input || e.summary || "").slice(0, 700)));
}

const ATTACH_MAX = 25 * 1024 * 1024;   // same cap as cp/uploads.py
const ATTACH_ACCEPT = ".png,.jpg,.jpeg,.gif,.webp,.heic,.pdf,.txt,.md,.csv,.tsv,.json,.log,.yaml,.yml,.docx,.xlsx,.pptx";
const fmtSize = (n) => (n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`);

function fmtCounts(c) {
  if (!c) return "";
  return [["user", "you"], ["text", "replies"], ["thinking", "thinking"], ["tool", "tool calls"]]
    .filter(([k]) => c[k]).map(([k, l]) => `${c[k]} ${l}`).join(", ");
}

function when(ts) { const t = Date.parse(ts); return isNaN(t) ? "" : ct(t / 1000, true); }

function entry(e) {
  const t = h("span", { class: "at" }, when(e.ts));
  if (e.kind === "user" && /^\s*<task-notification>/.test(e.text || "")) return taskEvent(e, t);
  if (e.kind === "user") return h("li", { class: "e user" + (e.command ? " cmd" : "") }, h("div", { class: "lbl" }, "You", t), h("div", { class: "txt" }, e.text));
  if (e.kind === "text") return h("li", { class: "e text" }, h("div", { class: "lbl" }, "Claude", t), md(e.text, "txt md"));
  if (e.kind === "thinking") return h("li", { class: "e thinking" },
    h("details", {}, h("summary", { class: "lbl" }, "Thinking", t, h("span", { class: "peek" }, e.redacted ? "redacted by the API" : e.text.slice(0, 140))),
      h("div", { class: "txt" }, e.redacted ? "(The API returned this thinking block encrypted; there is no text to show.)" : md(e.text, "md"))));
  if (e.kind === "tool") return h("li", { class: "e tool" + (e.error ? " err" : "") },
    h("details", {}, h("summary", { class: "lbl" }, h("b", {}, e.name), h("span", { class: "peek mono" }, e.summary || ""), e.error && h("span", { class: "chip risk-high" }, "error"),
      e.result == null && h("span", { class: "chip" }, "no result yet"), t),
      h("div", { class: "io" }, h("div", { class: "hint" }, "Input"), h("pre", {}, e.input),
        e.result != null && [h("div", { class: "hint" }, "Result"), h("pre", {}, e.result || "(empty)")])));
  if (e.kind === "summary") return h("li", { class: "e notice" }, h("details", {}, h("summary", { class: "lbl" }, "Compaction summary", t), md(e.text, "txt md")));
  return h("li", { class: "e notice" }, h("div", { class: "lbl" }, "Notice", t), h("div", { class: "txt" }, e.text));
}

export { age };

// A background task's completion notice is injected into the chat as a user turn, but Dev never typed it: show it
// as a one-line event (status and summary) that opens to the raw notice, not as "You" with raw XML.
function taskEvent(e, t) {
  const tag = (k) => { const m = new RegExp(`<${k}>([\\s\\S]*?)</${k}>`).exec(e.text); return m ? m[1].trim() : ""; };
  const status = tag("status"), summary = tag("summary") || tag("event") || "Background task update";
  return h("li", { class: "e notice task-ev" + (status === "failed" ? " err" : "") },
    h("details", {}, h("summary", { class: "lbl" }, status === "failed" ? "Task failed" : status ? `Task ${status}` : "Task event", t,
      h("span", { class: "ev-sum" }, summary.replace(/^Background command "?|"? completed \(exit code \d+\)$/g, ""))),
      h("pre", { class: "txt" }, e.text.trim())));
}
