// Chat history (#/chat/<session id>): the whole transcript, thinking and tool calls included, with the
// controls for that chat (reply, keep warm, stop, terminal). Live chats refresh in place every 5 s;
// a refresh never steals the scroll position or the reply being typed.
import { h, ct, age, toast } from "./lib.js";
import { md } from "./md.js";
import { get, post } from "./api.js";
import { micFor } from "./dictate.js";

const PAGE = 250;
const SHOW = { thinking: true, tools: true };

export class ChatView {
  constructor(el, ui) {
    this.el = el; this.ui = ui; this.sid = null; this.entries = []; this.start = 0; this.total = 0;
    this.timer = null; this.filter = "";
    this.show = { ...SHOW, ...(ui.store.get("chatShow", {})) };
  }

  async open(sid) {
    clearInterval(this.timer);
    if (sid !== this.sid) { this.sid = sid; this.entries = []; this.reply = ""; }
    this.built = null;
    this.el.replaceChildren(h("div", { class: "skeleton" }));
    try {
      const d = await get(`sessions/${encodeURIComponent(sid)}/transcript?limit=${PAGE}`);
      this.session = d.session; this.entries = d.entries; this.start = d.start; this.total = d.total; this.counts = d.counts;
    } catch (e) {
      this.el.replaceChildren(h("div", { class: "page" }, h("a", { href: "#/sessions", class: "hint" }, "Sessions"),
        h("div", { class: "empty" }, h("h3", {}, "Chat not found"), h("p", {}, e.message))));
      return;
    }
    this.render(true);
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
      const de = document.documentElement;
      const atBottom = de.scrollHeight - window.scrollY - window.innerHeight < 160;
      // keep older pages already loaded; replace the tail from the server
      const keep = this.entries.filter((e) => e.i < d.start);
      this.entries = keep.concat(d.entries);
      this.start = keep.length ? this.start : d.start;
      this.total = d.total; this.session = d.session; this.counts = d.counts;
      this.render(atBottom);
    } catch { /* the rail shows feed health; a missed poll is retried */ }
  }

  async older() {
    try {
      const d = await get(`sessions/${encodeURIComponent(this.sid)}/transcript?limit=${PAGE}&before=${this.start}`);
      const de = document.documentElement, prevH = de.scrollHeight, prevY = window.scrollY;
      this.entries = d.entries.concat(this.entries); this.start = d.start;
      this.render(false);
      window.scrollTo(0, prevY + de.scrollHeight - prevH);         // stay on the entry you were reading
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
          h("span", { class: "chip st-" + s.state }, s.live ? s.state : "not running"),
          h("span", { class: "chip" }, s.seat),
          s.work_item && h("a", { class: "chip wi", href: "#/work/" + encodeURIComponent(s.work_item) }, s.work_item),
          s.limited && h("span", { class: "chip risk-med" }, "at a limit"),
          s.cache_age_min != null && h("span", { class: "chip", title: "prompt cache lives 1 h from the last call" },
            `cache ${s.warmth || "?"}, last call ${Math.round(s.cache_age_min)} min ago`),
          kw && kw.on && h("span", { class: "chip risk-low", title: kw.detail || "" }, `keep-warm on, ${kw.pings} pings, stops in ${kw.stop_in_h} h`),
          ro && h("span", { class: "chip ro" }, "read-only seat"),
          h("span", { class: "hint" }, `${this.total} entries: ${fmtCounts(this.counts)}`))),
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
    const more = this.start > 0 && h("button", { class: "btn ghost older", onclick: () => this.older() }, `Load ${Math.min(PAGE, this.start)} earlier (${this.start} not loaded)`);
    const y = window.scrollY;
    // The page skeleton and the reply box are built once per chat; a refresh swaps the header and the log only, and
    // leaves the header alone while you type in its find box, so typing is never interrupted.
    const key = `${this.sid}:${ro}`;
    if (this.built !== key || !this.el.contains(this.pageEl)) {
      this.built = key;
      this.headSlot = h("div", { class: "chat-head-slot" });
      this.moreSlot = h("div", {});
      this.logSlot = h("div", {});
      this.box = ro ? h("div", { class: "chat-reply ro hint" }, "Read-only: this account is marked read-only in your chatdash config, so this board does not reply, stop or open it. Use that account's own terminal.")
        : h("div", { class: "chat-reply" },
          h("textarea", { class: "reply", rows: "3", "aria-label": "Reply", oninput: (e) => (this.reply = e.target.value),
            onkeydown: (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); this.send(); } } }),
          h("button", { class: "btn primary", onclick: () => this.send() }, "Send"));
      const ta = this.box.querySelector("textarea");
      if (ta) ta.after(micFor(ta));
      if (ta) ta.value = this.reply || "";
      this.pageEl = h("div", { class: "page chat" }, this.headSlot, h("div", { class: "chat-scroll" }, this.moreSlot, this.logSlot), this.box);
      this.el.replaceChildren(this.pageEl);
    }
    const ta = this.box.querySelector("textarea");
    if (ta) ta.placeholder = s.live ? "Reply to this chat (Cmd+Enter sends)" : "This chat is not running; a reply resumes it in the background";
    if (!this.headSlot.contains(document.activeElement) || !this.headSlot.firstChild) this.headSlot.replaceChildren(head);
    this.moreSlot.replaceChildren(more || "");
    this.logSlot.replaceChildren(this.log);
    this.paint();
    requestAnimationFrame(() => window.scrollTo(0, toBottom ? document.documentElement.scrollHeight : y));
  }

  paint() {
    const f = this.filter.trim().toLowerCase();
    const rows = this.entries.filter((e) => (this.show.thinking || e.kind !== "thinking") && (this.show.tools || e.kind !== "tool") &&
      (!f || [e.text, e.summary, e.input, e.result, e.name].some((v) => v && v.toLowerCase().includes(f))));
    this.log.replaceChildren(...(rows.length ? rows.map(entry) : [h("li", { class: "hint" }, f ? "Nothing matches in the loaded history" : "No entries")]));
  }

  async send() {
    const text = (this.reply || "").trim();
    if (!text) return;
    try {
      const r = await post(`sessions/${encodeURIComponent(this.session.session_id)}/reply`, { text, key: this.session.key });
      toast(r.queued ? "Queued: it is typed in when the chat finishes its turn" : "Sent");
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

function fmtCounts(c) {
  if (!c) return "";
  return [["user", "you"], ["text", "replies"], ["thinking", "thinking"], ["tool", "tool calls"]]
    .filter(([k]) => c[k]).map(([k, l]) => `${c[k]} ${l}`).join(", ");
}

function when(ts) { const t = Date.parse(ts); return isNaN(t) ? "" : ct(t / 1000, true); }

function entry(e) {
  const t = h("span", { class: "at" }, when(e.ts));
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
