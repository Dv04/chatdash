// B7 command palette (Cmd+K / Ctrl+K): fuzzy over commands, chats and work items; a typed sentence such as
// "reply continue to papers" is parsed by the grammar (no LLM) and previewed before anything happens.
import { h, toast } from "./lib.js";
import { post } from "./api.js";

function fuzzy(q, s) {
  q = q.toLowerCase(); s = s.toLowerCase();
  if (!q) return 1;
  if (s.includes(q)) return 2 - s.indexOf(q) / 1000;
  let i = 0, score = 0;
  for (const ch of s) { if (ch === q[i]) { i++; score++; } if (i === q.length) break; }
  return i === q.length ? score / s.length : 0;
}

export class Palette {
  constructor(ui) {
    this.ui = ui;
    this.dlg = document.body.appendChild(h("dialog", { class: "palette", "aria-label": "Command palette" }));
    this.input = h("input", { type: "text", class: "free pal-in", placeholder: "Type a command, a chat, a work item, or a sentence like: reply continue to papers",
      "aria-label": "Command", "aria-controls": "pal-list", "aria-autocomplete": "list", role: "combobox", "aria-expanded": "true" });
    this.preview = h("div", { class: "pal-preview", "aria-live": "polite" });
    this.list = h("ul", { class: "pal-list", id: "pal-list", role: "listbox" });
    this.dlg.append(this.input, this.preview, this.list, h("p", { class: "hint" }, "Enter runs, arrows move, Esc closes. Sends go out at once."));
    this.input.addEventListener("input", () => this.update());
    this.input.addEventListener("keydown", (e) => this.key(e));
    this.dlg.addEventListener("close", () => { this.input.value = ""; this.parsed = null; });
    this.sel = 0; this.items = []; this.parsed = null; this.timer = 0;
  }

  open() { this.dlg.showModal(); this.update(); this.input.focus(); }

  sources() {
    const u = this.ui, ov = u.overview() || { needs_you: [], capacity: { seats: [] } }, g = u.graphData() || { nodes: [] };
    const cmds = [
      ["Go to board", () => (location.hash = "#/")], ["Go to graph", () => (location.hash = "#/graph")],
      ["Review decisions", () => { location.hash = "#/"; u.toggleReview(); }], ["Briefing (read aloud)", () => u.voice.briefing()],
      ["Hands-free mode", () => u.voice.handsFree()], ["Talk to the dashboard", () => u.voice.converse()],
      ["Focus mode settings", () => u.toggleFocusPanel()], ["Switch theme", () => u.cycleTheme()], ["Refresh", () => u.refresh()],
    ].map(([label, run]) => ({ kind: "command", label, run }));
    const dec = ov.needs_you.map((x) => ({ kind: "needs you", label: `${x.title}`, hint: x.kind, run: () => u.jumpTo(x.session_id) }));
    const chats = g.nodes.filter((n) => n.type === "session").map((n) => ({ kind: "chat", label: n.label, hint: `${n.seat}, ${n.needs_you ? "needs you" : n.state}`,
      run: () => { location.hash = "#/graph"; setTimeout(() => u.openNode(n.id), 300); } }));
    const wis = g.nodes.filter((n) => n.type === "work_item").map((n) => ({ kind: "work item", label: n.label, hint: "open page",
      run: () => (location.hash = "#/work/" + encodeURIComponent(n.id.replace("work_item:", ""))) }));
    return [...cmds, ...dec, ...wis, ...chats];
  }

  update() {
    const q = this.input.value.trim();
    this.items = this.sources().map((it) => ({ ...it, s: fuzzy(q, it.label) })).filter((it) => it.s > 0)
      .sort((a, b) => b.s - a.s).slice(0, 12);
    this.sel = 0;
    this.render();
    clearTimeout(this.timer);
    this.parsed = null;
    this.preview.replaceChildren();
    if (q.split(/\s+/).length >= 2) this.timer = setTimeout(async () => {
      try {
        const r = await post("intent", { text: q, model: false });
        if (this.input.value.trim() !== q) return;
        this.parsed = r.cmd ? r : null;
        this.preview.replaceChildren(r.cmd ? h("div", { class: "pal-cmd" }, h("kbd", {}, "Enter"), " ", describe(r)) : "");
      } catch { /* preview is optional */ }
    }, 160);
  }

  render() {
    this.list.replaceChildren(...this.items.map((it, i) => h("li", { role: "option", "aria-selected": String(i === this.sel), class: i === this.sel ? "on" : "",
      onclick: () => this.run(it) }, h("span", { class: "chip" }, it.kind), " ", it.label, it.hint && h("span", { class: "hint" }, "  " + it.hint))));
  }

  key(e) {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      this.sel = Math.max(0, Math.min(this.items.length - 1, this.sel + (e.key === "ArrowDown" ? 1 : -1)));
      this.render();
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (this.parsed) this.exec(this.parsed); else if (this.items[this.sel]) this.run(this.items[this.sel]);
    }
  }

  run(it) { this.dlg.close(); it.run(); }

  exec(r) { this.dlg.close(); this.ui.runIntent(r); }
}

export function describe(r) {
  switch (r.cmd) {
    case "reply": return `Reply "${r.text}" to ${r.chat}`;
    case "stop": return `Stop ${r.chat}`;
    case "spawn": return `Start a session on ${r.work_item}${r.seat ? " on " + r.seat : " (pick the seat)"}`;
    case "answer": return `Answer ${r.decision ? "decision " + r.decision : "the current decision"} with option ${r.option}`;
    case "open": return `Open ${r.target}`;
    case "needs_you": return "Read what needs you";
    case "status": return "Read seat status";
    default: return r.cmd;
  }
}
export { toast };
