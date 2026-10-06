// B5 graph: seats, work items, sessions, jobs and subagents as one live picture.
// Canvas, drawn only while something moves (100+ nodes at 60 fps). Color = state, area = spend,
// ring = needs you. Layouts: force, seat swimlanes. Views: agent tree (runs_on, spawned_by) and work
// graph (belongs_to, collides_with, waits_on). Idle sessions cluster into "N idle" until expanded.
import { h, ct, age, toast } from "./lib.js";
import { get } from "./api.js";
import { micFor } from "./dictate.js";
import { renderMode, MODES, filesBrowser } from "./gx.js";
import { md } from "./md.js";

const KIND_EDGES = { tree: new Set(["runs_on", "spawned_by"]), work: new Set(["belongs_to", "collides_with", "waits_on", "spawned_by"]),
  repo: new Set(["in_repo", "spawned_by", "collides_with"]) };
const LAYER_EDGES = new Set(["touched", "opened", "pinned"]);
const STATES = ["needs_you", "working", "idle", "stopped", "unknown"];

function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

export class Graph {
  constructor(root, ui) {
    this.root = root; this.ui = ui;
    this.layout = ui.store.get("graphLayout", "force");
    this.view = ui.store.get("graphView", "tree");
    this.filters = { seats: new Set(), states: new Set(), q: "" };
    this.showFilters = false;
    this.expanded = new Set();
    this.pos = new Map();            // id -> {x, y, vx, vy, fixed}
    this.t = { k: 1, x: 0, y: 0 };
    this.hover = null; this.focus = null; this.drag = null;
    this.alpha = 0; this.raf = 0; this.frames = []; this.autoFit = true;
    this.mode = ui.store.get("graphMode", "map");
    this.layers = new Set(ui.store.get("graphLayers", []));
    this.pins = new Map();           // path -> {path, label, dir, sessions}
    this.hot = []; this.prs = []; this.hl = null; this.hlFiles = null; this.changes = new Map(); this.flash = new Set();
    this.build();
    this.loadLayers();
    if (this.mode !== "map") setTimeout(() => this.setMode(this.mode), 0);
  }

  // ------------------------------------------------------------------ DOM
  build() {
    const seg = (name, opts, cur, set) => h("div", { class: "seg", role: "group", "aria-label": name },
      opts.map(([v, l]) => h("button", { "aria-pressed": String(cur === v), onclick: () => set(v) }, l)));
    this.bar = h("div", { class: "g-bar" });
    this.canvas = h("canvas", { class: "g-canvas", tabindex: "0", role: "application",
      "aria-label": "Graph of seats, work items and sessions. Arrow keys move between nodes, Enter opens one, T toggles a table view." });
    this.tip = h("div", { class: "g-tip", role: "tooltip", hidden: true });
    this.panel = h("aside", { class: "g-panel", hidden: true, "aria-label": "Node details" });
    this.menu = h("div", { class: "g-menu", role: "menu", hidden: true });
    this.mini = h("canvas", { class: "g-mini", width: 180, height: 120, "aria-hidden": "true" });
    this.table = h("div", { class: "g-table", hidden: true });
    this.stage = h("div", { class: "g-stage" }, this.canvas, this.mini, this.tip, this.menu);
    this.body = h("div", { class: "g-body" }, this.stage, this.panel);
    this.vw = h("div", { class: "gx-view", hidden: true });
    this.root.replaceChildren(this.bar, this.body, this.table, this.vw);
    this.modeSeg = () => seg("Mode", MODES, this.mode, (v) => this.setMode(v));
    this.segs = () => [
      seg("Layout", [["force", "force"], ["lanes", "seat lanes"]], this.layout, (v) => { this.layout = v; this.ui.store.set("graphLayout", v); this.relayout(); }),
      seg("Group", [["tree", "by seat"], ["work", "by work item"], ["repo", "by repo"]], this.view, (v) => { this.view = v; this.ui.store.set("graphView", v); this.renderBar(); this.relayout(); }),
    ];
    this.ctx = this.canvas.getContext("2d");
    this.mctx = this.mini.getContext("2d");
    new ResizeObserver(() => this.resize()).observe(this.stage);
    this.bind();
  }

  setMode(v) {
    this.mode = v; this.ui.store.set("graphMode", v);
    this.body.hidden = v !== "map"; this.vw.hidden = v === "map"; this.table.hidden = true;
    this.renderBar();
    if (v === "map") { this.relayout(); this.resize(); } else renderMode(this.vw, v, this);
  }

  async loadLayers() {
    try {
      if (this.layers.has("files")) this.hot = (await get("gx/hot?window=" + (this.ui.store.get("hotWindow", 86400)))).files || [];
      if (this.layers.has("prs")) this.prs = (await get("gx/prs")).prs || [];
    } catch (e) { toast(`Layer not loaded: ${e.message}`); }
    if (this.mode === "map") this.relayout();
    clearTimeout(this.layerTimer);
    this.layerTimer = setTimeout(() => this.loadLayers(), 60000);
  }
  toggleLayer(k) {
    this.layers.has(k) ? this.layers.delete(k) : this.layers.add(k);
    this.ui.store.set("graphLayers", [...this.layers]);
    this.renderBar(); this.loadLayers();
  }
  async searchAll(q) {
    q = (q || "").trim();
    if (!q) { this.hl = null; this.hlFiles = null; this.searchRes = null; this.renderBar(); this.relayout(); return; }
    try {
      const r = await get("gx/search?q=" + encodeURIComponent(q));
      this.searchRes = r; this.hl = new Set(r.matched_sessions); this.hlFiles = new Set(r.files.map((f) => f.path));
      for (const f of r.files.slice(0, 8)) if (!this.pins.has(f.path)) this.pins.set(f.path, { path: f.path, label: f.label.split("/").pop(), dir: false, sessions: f.sessions, fromSearch: true });
      toast(`${r.matched_sessions.length} chats, ${r.files.length} files, ${r.prs.length} PRs match`);
    } catch (e) { toast(`Search failed: ${e.message}`); }
    this.renderBar(); this.relayout();
  }
  clearSearch() { for (const [k, v] of this.pins) if (v.fromSearch) this.pins.delete(k); this.searchAll(""); }
  pin(item) { this.pins.set(item.path, { ...item, fromSearch: false }); this.relayout(); toast(`${item.label} pinned to the graph`); }
  unpin(path) { this.pins.delete(path); this.relayout(); }
  newChat(opts = {}) { this.ui.spawn(opts); }

  renderBar() {
    if (this.mode !== "map") {
      // same swipeable mode row as the map bar (it used to wrap "cost per outcome" and clip "overlaps" on a phone)
      this.bar.replaceChildren(h("div", { class: "g-row g-row-views" }, this.modeSeg()), h("button", { class: "btn", onclick: () => this.newChat() }, "+ New chat"));
      this.revealMode();
      return;
    }
    const seats = [...new Set((this.data?.nodes || []).filter((n) => n.type === "seat").map((n) => n.seat))];
    const chip = (set, v, label) => h("button", { class: "chip toggle", "aria-pressed": String(set.has(v)),
      onclick: () => { set.has(v) ? set.delete(v) : set.add(v); this.relayout(); this.renderBar(); } }, label);
    const q = h("input", { type: "search", class: "free g-q", placeholder: "Filter (/)", value: this.filters.q, "aria-label": "Filter nodes",
      oninput: (e) => { this.filters.q = e.target.value.toLowerCase(); this.relayout(); } });
    const sq = h("input", { type: "search", class: "free g-sq", placeholder: "Search files, PRs, chats", title: "Search files, PRs and chats (Enter)", value: this.sq || "",
      "aria-label": "Search everything", onkeydown: (e) => { if (e.key === "Enter") { this.sq = e.target.value; this.searchAll(this.sq); } } });
    const nOn = this.filters.seats.size + this.filters.states.size;
    const layer = (k, l) => h("button", { class: "chip toggle", "aria-pressed": String(this.layers.has(k)), onclick: () => this.toggleLayer(k) }, l);
    // g-row wrappers are display: contents on desktop (same flow as before); on a phone each is one swipeable line
    const row = (cls, ...kids) => h("div", { class: "g-row " + cls }, ...kids);
    this.bar.replaceChildren(...[row("g-row-views", this.modeSeg(), ...this.segs(),
      h("div", { class: "g-filters", role: "group", "aria-label": "Layers" }, layer("files", "hot files"), layer("prs", "PRs"),
        this.pins.size > 0 && h("button", { class: "chip toggle", onclick: () => { this.pins.clear(); this.relayout(); this.renderBar(); } }, `clear ${this.pins.size} pinned`))),
      row("g-row-search", sq, this.hl && h("button", { class: "btn ghost", onclick: () => { this.sq = ""; this.clearSearch(); } }, `Clear search (${this.hl.size})`),
        h("button", { class: "btn", onclick: () => this.newChat() }, "+ New chat")),
      row("g-row-tools", q,
        h("button", { class: "btn ghost", "aria-pressed": String(this.showFilters), "aria-expanded": String(this.showFilters),
          title: "Filter by seat and state", onclick: () => { this.showFilters = !this.showFilters; this.renderBar(); } },
          nOn ? `Filters · ${nOn}` : "Filters"),
        h("button", { class: "btn ghost", onclick: () => { this.userMoved = false; this.fit(); }, title: "Fit (0)" }, "Fit"),
        h("button", { class: "btn ghost", "aria-pressed": String(!this.table.hidden), onclick: () => this.toggleTable() }, "Table"),
        h("button", { class: "btn ghost", "aria-pressed": String(!!this.frames24), onclick: () => this.toggleReplay() }, "Replay"),
        h("span", { class: "hint g-legend" }, "color = state, size = spend today, red ring = needs you, dashed = unknown; squares = files, hexagons = PRs")),
      // seat and state chips stay folded behind Filters (with a count when any is on) so the bar is two lines, not four
      this.showFilters && row("g-row-filters", h("div", { class: "g-filters", role: "group", "aria-label": "Seats" }, seats.map((s) => chip(this.filters.seats, s, s))),
        h("div", { class: "g-filters", role: "group", "aria-label": "States" }, STATES.map((s) => chip(this.filters.states, s, s.replace("_", " ")))),
        nOn > 0 && h("button", { class: "chip toggle", onclick: () => { this.filters.seats.clear(); this.filters.states.clear(); this.relayout(); this.renderBar(); } }, "clear filters")),
      this.frames24 && this.replayBar()].filter((x) => x != null && x !== false));
    this.revealMode();
  }
  // on a phone the mode row scrolls sideways: bring the chosen mode into view so it is never half off the edge
  revealMode() {
    requestAnimationFrame(() => {
      const on = this.bar.querySelector('.g-row-views .seg button[aria-pressed="true"]'), row = on && on.closest(".g-row-views");
      if (!row || row.scrollWidth <= row.clientWidth) return;
      const d = on.getBoundingClientRect(), r = row.getBoundingClientRect();
      if (d.left < r.left + 12 || d.right > r.right - 40) row.scrollLeft += d.left - r.left - 24;
    });
  }

  // ------------------------------------------------------------------ B10 day replay
  async toggleReplay() {
    if (this.frames24) { this.stopPlay(); this.frames24 = null; this.replay = null; this.changes = new Map(); this.flash = new Set(); this.diff = null; this.renderBar(); this.relayout(); return; }
    try { this.frames24 = (await get("graph/history?hours=24")).frames || []; }
    catch (e) { toast(`No history: ${e.message}`); return; }
    if (!this.frames24.length) { this.frames24 = null; toast("No history yet: frames are recorded once a minute"); return; }
    this.setFrame(this.frames24.length - 1);
  }
  // Replay with changes: each step is compared with the previous frame (started, ended, state changed) and
  // the chats that wrote files in between are lit, with the files themselves when the hot-files layer is on.
  setFrame(i, step = 1) {
    const f = this.frames24[i], prev = this.frames24[Math.max(0, i - step)];
    this.replayIdx = i;
    this.replay = new Map(f.nodes.map((n) => [n[0], { state: n[1], needs: !!n[2] }]));
    const before = new Map(prev.nodes.map((n) => [n[0], { state: n[1], needs: !!n[2] }]));
    this.changes = new Map();
    let born = 0, gone = 0, changed = 0;
    if (prev !== f) {
      for (const [id, v] of this.replay) {
        const b = before.get(id);
        if (!b) { this.changes.set(id, "born"); born++; }
        else if (b.state !== v.state || b.needs !== v.needs) { this.changes.set(id, "changed"); changed++; }
      }
      for (const id of before.keys()) if (!this.replay.has(id)) { this.changes.set(id, "gone"); gone++; }
    }
    this.diff = { born, gone, changed, writes: null };
    this.flash = new Set();
    const seq = (this.touchSeq = (this.touchSeq || 0) + 1);
    if (prev !== f) get(`gx/touches?since=${prev.at}&until=${f.at}`).then((r) => {
      if (seq !== this.touchSeq) return;
      const t = r.touches || [];
      this.flash = new Set(t.map((x) => x.session_id).concat(t.map((x) => "file:" + x.norm)));
      this.diff.writes = t.length; this.diff.files = new Set(t.map((x) => x.norm)).size;
      this.renderBar(); this.relayout();
    }).catch(() => {});
    this.renderBar();
    this.relayout();
  }
  play() {
    if (this.playing) { this.stopPlay(); this.renderBar(); return; }
    const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (this.replayIdx >= this.frames24.length - 1) this.replayIdx = 0;
    this.playing = setInterval(() => {
      const step = 5;                                    // 5 one-minute frames per tick
      const next = Math.min(this.frames24.length - 1, this.replayIdx + step);
      this.setFrame(next, step);
      if (next >= this.frames24.length - 1) { this.stopPlay(); this.renderBar(); }
    }, reduce ? 1500 : 700);
    this.renderBar();
  }
  stopPlay() { clearInterval(this.playing); this.playing = null; }
  replayBar() {
    const n = this.frames24.length, f = this.frames24[this.replayIdx], d = this.diff || {};
    const r = h("input", { type: "range", min: "0", max: String(n - 1), value: String(this.replayIdx), class: "g-scrub",
      "aria-label": "Replay time", "aria-valuetext": ct(f.at, true), oninput: (e) => { this.stopPlay(); this.setFrame(Number(e.target.value)); } });
    const summary = [d.born && `+${d.born} started`, d.gone && `${d.gone} ended`, d.changed && `${d.changed} changed state`,
      d.writes != null && `${d.writes} file writes${d.files ? ` in ${d.files} files` : ""}`].filter(Boolean).join(", ") || "no change in this step";
    return h("span", { class: "g-replay" },
      h("button", { class: "btn ghost", "aria-pressed": String(!!this.playing), onclick: () => this.play() }, this.playing ? "Pause" : "Play"),
      r, h("span", { class: "hint" }, `${ct(f.at, true)}: ${summary}`),
      h("span", { class: "hint g-legend" }, "green ring started, blue ring changed, dashed ended, glow wrote files"));
  }

  // ------------------------------------------------------------------ data -> visible model
  update(data) {
    const first = !this.data;
    this.data = data;
    if (first || !this.bar.contains(document.activeElement)) this.renderBar();   // never rebuild under the caret
    this.relayout(first);
  }

  model() {
    const d = this.data || { nodes: [], edges: [] };
    const kinds = KIND_EDGES[this.view];
    const f = this.filters;
    let nodes = d.nodes.filter((n) => {
      if (this.view === "tree" && n.type === "work_item") return false;
      if (this.view === "work" && n.type === "seat") return false;
      if (this.view === "repo" && (n.type === "seat" || n.type === "work_item")) return false;
      if (f.seats.size && n.seat && !f.seats.has(n.seat)) return false;
      if (f.states.size && n.type !== "seat" && n.type !== "work_item" && !f.states.has(n.needs_you ? "needs_you" : n.state)) return false;
      if (f.q && !(n.label || "").toLowerCase().includes(f.q) && n.type !== "seat" && n.type !== "work_item") return false;
      return true;
    });
    // idle clusters: per seat (tree) or per work item (work), 2+ idle sessions fold into one node
    const groupOf = (n) => (this.view === "tree" ? `seat:${n.seat}` : this.view === "repo" ? `repo:${n.repo || "unknown"}` : n.parent || "none");
    const extra = [];
    if (this.view === "repo") {
      const repos = new Map();
      for (const n of nodes) if (n.type === "session") {
        const r = n.repo || "unknown";
        if (!repos.has(r)) repos.set(r, []);
        repos.get(r).push(n);
        extra.push({ from: n.id, to: `repo:${r}`, kind: "in_repo" });
      }
      for (const [r, list] of repos) nodes.push({ id: `repo:${r}`, type: "repo", label: r === "unknown" ? "unknown folder" : r.split("/").slice(-2).join("/"),
        path: r, state: list.some((n) => n.needs_you) ? "needs_you" : list.some((n) => n.state === "working") ? "working" : "idle",
        spend: list.reduce((t, n) => t + (n.spend || 0), 0), members: list.length });
    }
    const idle = new Map();
    for (const n of nodes) if (n.type === "session" && n.state === "idle" && !n.needs_you) {
      const g = groupOf(n);
      if (!this.expanded.has(g)) { if (!idle.has(g)) idle.set(g, []); idle.get(g).push(n); }
    }
    const folded = new Map();
    for (const [g, list] of idle) if (list.length >= 2) {
      const id = `cluster:${g}`;
      list.forEach((n) => folded.set(n.id, id));
      nodes.push({ id, type: "cluster", label: `${list.length} idle`, state: "idle", group: g, members: list,
        spend: list.reduce((t, n) => t + (n.spend || 0), 0), seat: list[0].seat });
    }
    nodes = nodes.filter((n) => !folded.has(n.id));
    // layers: hot files, pinned files and folders, PRs; each links to the sessions (or their idle cluster) in view
    const sidNode = new Map();
    for (const n of d.nodes) if (n.type === "session") sidNode.set(n.session_id, folded.get(n.id) || n.id);
    const present = new Set(nodes.map((n) => n.id));
    const linkFile = (f, id, kind) => {
      const targets = [...new Set((f.sessions || []).map((sid) => sidNode.get(sid)).filter((x) => x && present.has(x)))];
      return targets.map((t) => ({ from: t, to: id, kind }));
    };
    const addFile = (f, extraProps, kind) => {
      const id = "file:" + f.path;
      if (present.has(id)) return;
      const es = linkFile(f, id, kind);
      if (!es.length && !extraProps.pinned) return;
      nodes.push({ id, type: f.dir ? "folder" : "file", label: f.label || f.path.split("/").pop(), path: f.path, sessions: f.sessions || [],
        state: "idle", spend: 0, n_sessions: f.n_sessions || (f.sessions || []).length, files: f.files, ...extraProps });
      present.add(id); extra.push(...es);
    };
    if (this.layers.has("files")) for (const f of this.hot) addFile(f, { hot: true }, "touched");
    for (const f of this.pins.values()) addFile(f, { pinned: true }, "pinned");
    if (this.layers.has("prs")) {
      const byUrl = new Map();
      // open or not yet read only, at most 5 per chat (the newest numbers): the PRs view lists them all
      const perSess = new Map();
      for (const p of [...this.prs].filter((p) => p.state === "open" || p.state === "unknown").sort((a, b) => b.num - a.num)) {
        const n = perSess.get(p.session_id) || 0;
        if (n >= 5) continue;
        perSess.set(p.session_id, n + 1);
        if (!byUrl.has(p.url)) byUrl.set(p.url, { ...p, sessions: [] });
        byUrl.get(p.url).sessions.push(p.session_id);
      }
      for (const p of byUrl.values()) {
        const id = "pr:" + p.url, es = linkFile(p, id, "opened");
        if (!es.length) continue;
        nodes.push({ id, type: "pr", label: `#${p.num} ${p.repo.split("/").pop()}`, url: p.url, state: "pr_" + p.state, sessions: p.sessions, spend: 0 });
        present.add(id); extra.push(...es);
      }
    }
    const ids = new Set(nodes.map((n) => n.id));
    const seen = new Set();
    const edges = [];
    for (const e of d.edges.concat(extra)) {
      if (!kinds.has(e.kind) && !LAYER_EDGES.has(e.kind)) continue;
      const a = folded.get(e.from) || e.from, b = folded.get(e.to) || e.to;
      const key = `${a}>${b}>${e.kind}`;
      if (a === b || !ids.has(a) || !ids.has(b) || seen.has(key)) continue;
      seen.add(key);
      edges.push({ from: a, to: b, kind: e.kind });
    }
    for (const n of nodes) if (n.type === "cluster") {
      const target = n.group;
      if (ids.has(target)) edges.push({ from: n.id, to: target, kind: this.view === "tree" ? "runs_on" : this.view === "repo" ? "in_repo" : "belongs_to" });
    }
    if (this.replay) {
      nodes = nodes.map((n) => {
        const r = this.replay.get(n.id);
        return r ? { ...n, state: r.state, needs_you: r.needs, ghost: false } : n.type === "cluster" ? n : { ...n, ghost: true };
      });
    }
    if (this.hl) for (const n of nodes) {
      n.dim = n.type === "session" ? !this.hl.has(n.session_id)
        : n.type === "cluster" ? !n.members.some((m) => this.hl.has(m.session_id))
        : n.type === "file" || n.type === "folder" ? !(this.hlFiles && this.hlFiles.has(n.path)) && !(n.sessions || []).some((x) => this.hl.has(x))
        : n.type === "pr" ? !(n.sessions || []).some((x) => this.hl.has(x)) : false;
    }
    for (const n of nodes) { n.change = this.changes.get(n.id) || null; n.flashing = n.session_id ? this.flash.has(n.session_id) : this.flash.has(n.id); }
    const maxSpend = Math.max(1, ...nodes.filter((n) => n.type !== "seat").map((n) => n.spend || 0));
    for (const n of nodes) n.r = radius(n, maxSpend);
    return { nodes, edges, byId: new Map(nodes.map((n) => [n.id, n])) };
  }

  relayout(first) {
    this.m = this.model();
    const W = this.W || 1000, H = this.H || 700;
    for (const n of this.m.nodes) if (!this.pos.has(n.id)) {
      const p = n.parent && this.pos.get(n.parent);
      this.pos.set(n.id, { x: (p ? p.x : W / 2) + (Math.random() - 0.5) * 80, y: (p ? p.y : H / 2) + (Math.random() - 0.5) * 80, vx: 0, vy: 0 });
    }
    if (this.layout === "lanes") this.lanes();
    this.alpha = this.layout === "force" ? Math.max(this.alpha, first ? 1 : 0.35) : 0.6;
    if (!this.table.hidden) this.renderTable();
    this.kick();
    if (first) this.autoFit = true;
  }

  // ------------------------------------------------------------------ layouts
  lanes() {
    const seats = this.m.nodes.filter((n) => n.type === "seat");
    const wis = this.m.nodes.filter((n) => n.type === "work_item");
    const laneW = 170, x0 = wis.length ? 220 : 60;
    const laneOf = new Map(seats.map((s, i) => [s.seat, i]));
    const col = new Map();
    for (const s of seats) this.target(s.id, x0 + laneOf.get(s.seat) * laneW, 40);
    const rows = this.m.nodes.filter((n) => n.type === "session" || n.type === "cluster")
      .sort((a, b) => rank(a) - rank(b) || (b.activity || 0) - (a.activity || 0));
    const ys = new Map();
    for (const n of rows) {
      const i = laneOf.has(n.seat) ? laneOf.get(n.seat) : seats.length;
      const k = col.get(i) || 0;
      col.set(i, k + 1);
      const y = 110 + k * 46;
      this.target(n.id, x0 + i * laneW, y);
      ys.set(n.id, y);
    }
    for (const n of this.m.nodes.filter((n) => n.type === "job")) {
      const p = this.pos.get(n.parent) || { x: x0, y: 110 };
      this.target(n.id, p.tx != null ? p.tx + 34 : p.x + 34, (p.ty != null ? p.ty : p.y) + 16);
    }
    wis.forEach((w, i) => {
      const mine = this.m.edges.filter((e) => e.to === w.id).map((e) => ys.get(e.from)).filter((y) => y != null);
      this.target(w.id, 60, mine.length ? mine.reduce((a, b) => a + b) / mine.length : 110 + i * 46);
    });
  }
  target(id, x, y) { const p = this.pos.get(id); if (p) { p.tx = x; p.ty = y; } }

  step() {
    const nodes = this.m.nodes, P = this.pos, a = this.alpha;
    if (this.layout === "lanes") {
      let moving = 0;
      for (const n of nodes) { const p = P.get(n.id); if (p.tx == null) continue;
        const dx = p.tx - p.x, dy = p.ty - p.y; p.x += dx * 0.18; p.y += dy * 0.18; moving += Math.abs(dx) + Math.abs(dy); }
      this.alpha = moving > 1 ? a : 0;
      return;
    }
    for (const n of nodes) { const p = P.get(n.id); p.tx = p.ty = null; }
    const cx = (this.W || 1000) / 2, cy = (this.H || 700) / 2;   // the first frame can run before ResizeObserver
    const rep = 900 * Math.max(0.25, Math.min(1, ((this.W || 1000) / 1300) ** 2));   // a phone stage packs tighter
    for (let i = 0; i < nodes.length; i++) {
      const pi = P.get(nodes[i].id);
      for (let j = i + 1; j < nodes.length; j++) {
        const pj = P.get(nodes[j].id);
        let dx = pi.x - pj.x, dy = pi.y - pj.y, d2 = dx * dx + dy * dy;
        if (d2 > 160000) continue;
        if (d2 < 1) { dx = Math.random(); dy = Math.random(); d2 = 1; }
        const f = (rep * a) / d2;
        pi.vx += dx * f; pi.vy += dy * f; pj.vx -= dx * f; pj.vy -= dy * f;
      }
    }
    for (const e of this.m.edges) {
      const s = P.get(e.from), t = P.get(e.to);
      const rest = e.kind === "spawned_by" ? 36 : e.kind === "runs_on" ? 110 : e.kind === "touched" || e.kind === "pinned" || e.kind === "opened" ? 60 : 90;
      const dx = t.x - s.x, dy = t.y - s.y, d = Math.sqrt(dx * dx + dy * dy) || 1;
      const k = ((d - rest) / d) * 0.06 * a;
      s.vx += dx * k; s.vy += dy * k; t.vx -= dx * k; t.vy -= dy * k;
    }
    for (const n of nodes) {
      const p = P.get(n.id);
      if (p.fixed || (this.drag && this.drag.node === n)) { p.vx = p.vy = 0; continue; }
      p.vx += (cx - p.x) * 0.012 * a; p.vy += (cy - p.y) * 0.012 * a;
      p.vx *= 0.82; p.vy *= 0.82;
      p.x += Math.max(-30, Math.min(30, p.vx)); p.y += Math.max(-30, Math.min(30, p.vy));
    }
    this.alpha = a * 0.985;
    if (this.alpha < 0.01) this.alpha = 0;
    for (const n of nodes) {                     // never let one bad frame poison the layout
      const p = P.get(n.id);
      if (!Number.isFinite(p.x) || !Number.isFinite(p.y)) { p.x = cx + (Math.random() - 0.5) * 200; p.y = cy + (Math.random() - 0.5) * 200; p.vx = p.vy = 0; }
    }
  }

  // ------------------------------------------------------------------ drawing
  kick() { if (!this.raf) this.raf = requestAnimationFrame((t) => this.frame(t)); }
  frame(t) {
    this.raf = 0;
    if (this.last) { this.frames.push(t - this.last); if (this.frames.length > 120) this.frames.shift(); }
    this.last = t;
    if (this.alpha > 0) this.step();
    // Keep the whole graph in view while the layout settles (it used to fit once mid-settle, then drift past the
    // frame and under the minimap); stops for good once it is still or the person pans, zooms or drags.
    if (this.autoFit && this.m && this.m.nodes.length && this.alpha < 0.3) { this.fit(); if (this.alpha <= 0) this.autoFit = false; }
    this.draw();
    if (this.alpha > 0 || this.drag || this.pulse()) this.kick(); else this.last = 0;
  }
  pulse() { return false; }       // the needs-you ring is static: reduced motion by default
  fps() { if (!this.frames.length) return null; const avg = this.frames.reduce((a, b) => a + b) / this.frames.length; return Math.round(1000 / avg); }

  resize() {
    const r = this.stage.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
    this.W = r.width; this.H = r.height;
    this.canvas.width = r.width * dpr; this.canvas.height = r.height * dpr;
    this.canvas.style.width = r.width + "px"; this.canvas.style.height = r.height + "px";
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    if (!this.userMoved && this.m && this.m.nodes.length) this.fit();   // e.g. the table opened under the map
    this.kick();
  }

  colors() {
    return { working: css("--action"), needs_you: css("--bad"), idle: css("--idle"), stopped: css("--idle"), unknown: css("--warn"),
      limited: css("--warn"), ok: css("--ok"), near: css("--warn"), blocked: css("--bad"), line: css("--line-strong"),
      ink: css("--ink"), ink2: css("--ink-2"), ink3: css("--ink-3"), bg: css("--panel"), focus: css("--focus"), panel2: css("--panel-2"),
      pr_merged: css("--ok"), pr_open: css("--action"), pr_closed: css("--idle"), pr_unknown: css("--warn") };
  }

  draw() {
    if (!this.m || !this.W) return;          // resize can fire before the first model is built
    const c = this.ctx, C = this.colors(), t = this.t, P = this.pos;
    c.save();
    c.clearRect(0, 0, this.W, this.H);
    c.translate(t.x, t.y); c.scale(t.k, t.k);
    for (const e of this.m.edges) {
      const s = P.get(e.from), d = P.get(e.to);
      c.beginPath(); c.moveTo(s.x, s.y); c.lineTo(d.x, d.y);
      const a = this.m.byId.get(e.from), b = this.m.byId.get(e.to);
      const dimE = (a && a.dim) || (b && b.dim);
      c.lineWidth = (e.kind === "collides_with" ? 2 : 1) / t.k;
      c.strokeStyle = e.kind === "collides_with" ? C.blocked : e.kind === "waits_on" ? C.unknown : e.kind === "pinned" ? C.focus : C.line;
      c.setLineDash(e.kind === "belongs_to" || e.kind === "waits_on" || e.kind === "in_repo" ? [4 / t.k, 4 / t.k]
        : e.kind === "touched" || e.kind === "pinned" ? [1.5 / t.k, 3 / t.k] : []);
      c.globalAlpha = dimE ? 0.08 : e.kind === "runs_on" || e.kind === "belongs_to" || e.kind === "in_repo" ? 0.55 : 0.9;
      c.stroke();
    }
    c.setLineDash([]); c.globalAlpha = 1;
    const show = this.labelSet(c, t, P);
    // seat boxes go under the dots (a chat dot that lands on a seat stays visible) and their text goes on top of both
    for (const n of [...this.m.nodes].sort((a, b) => (b.type === "seat") - (a.type === "seat"))) {
      const p = P.get(n.id), r = n.r;
      c.globalAlpha = n.ghost ? 0.18 : n.dim ? 0.13 : 1;
      const fill = n.type === "file" || n.type === "folder" ? (n.pinned ? C.focus : n.hot && n.n_sessions > 1 ? C.unknown : C.ink3)
        : n.type === "seat" ? (C[n.state] || C.idle) : n.limited ? C.limited : (C[n.needs_you ? "needs_you" : n.state] || C.unknown);
      c.beginPath();
      if (n.type === "file") c.rect(p.x - r * 0.8, p.y - r, r * 1.6, r * 2);
      else if (n.type === "folder") roundRect(c, p.x - r * 1.3, p.y - r * 0.9, r * 2.6, r * 1.8, 3);
      else if (n.type === "pr") { for (let i = 0; i < 6; i++) { const an = Math.PI / 3 * i + Math.PI / 6; c[i ? "lineTo" : "moveTo"](p.x + r * Math.cos(an), p.y + r * Math.sin(an)); } c.closePath(); }
      else if (n.type === "repo") roundRect(c, p.x - r, p.y - r, r * 2, r * 2, 4);
      else if (n.type === "seat") {
        c.font = `600 ${12 / t.k}px -apple-system, system-ui, sans-serif`;
        const w = Math.max(r * 3.2, c.measureText(n.label).width + 16 / t.k);
        const bh = Math.max(r * 1.6, 20 / t.k);
        roundRect(c, p.x - w / 2, p.y - bh / 2, w, bh, 6 / t.k);
      }
      else if (n.type === "work_item") { c.moveTo(p.x, p.y - r); c.lineTo(p.x + r, p.y); c.lineTo(p.x, p.y + r); c.lineTo(p.x - r, p.y); c.closePath(); }
      else if (n.type === "job") c.rect(p.x - r, p.y - r, r * 2, r * 2);
      else c.arc(p.x, p.y, r, 0, Math.PI * 2);
      const hollow = n.state === "stopped" || n.type === "cluster" || n.type === "seat" || n.type === "work_item" || n.type === "repo" || n.type === "folder";
      c.fillStyle = hollow ? C.bg : fill; c.fill();
      c.lineWidth = (n.type === "seat" ? 2 : 1.5) / t.k;
      c.strokeStyle = n.state === "unknown" || n.excluded ? C.unknown : fill;
      c.setLineDash(n.state === "unknown" || n.state === "pr_unknown" ? [3 / t.k, 2 / t.k] : n.excluded ? [5 / t.k, 3 / t.k] : []);
      c.stroke(); c.setLineDash([]);
      if (n.needs_you) { c.beginPath(); c.arc(p.x, p.y, r + 4 / t.k + 2, 0, Math.PI * 2); c.lineWidth = 2.5 / t.k; c.strokeStyle = C.needs_you; c.stroke(); }
      if (n.change) { c.beginPath(); c.arc(p.x, p.y, r + 7 / t.k + 2, 0, Math.PI * 2); c.lineWidth = 2 / t.k;
        c.strokeStyle = n.change === "born" ? C.ok : n.change === "gone" ? C.idle : C.focus; c.setLineDash(n.change === "gone" ? [3 / t.k, 3 / t.k] : []); c.stroke(); c.setLineDash([]); }
      if (n.flashing) { c.beginPath(); c.arc(p.x, p.y, r + 11 / t.k + 2, 0, Math.PI * 2); c.lineWidth = 3 / t.k; c.strokeStyle = C.working; c.globalAlpha = Math.min(c.globalAlpha, 0.7); c.stroke(); c.globalAlpha = n.dim ? 0.13 : 1; }
      if (this.focus === n.id || this.hover === n.id) { c.beginPath(); c.arc(p.x, p.y, r + 8 / t.k + 2, 0, Math.PI * 2); c.lineWidth = 2 / t.k; c.strokeStyle = C.focus; c.stroke(); }
      if (show.has(n.id) && n.type !== "seat") {
        const bold = n.type === "work_item" || n.type === "repo";
        c.fillStyle = bold ? C.ink : C.ink2;
        c.font = `${bold ? 600 : 400} ${(n.type === "file" || n.type === "pr" ? 11 : 12) / t.k}px -apple-system, system-ui, sans-serif`;
        const lb = show.get(n.id);
        c.textAlign = lb.left ? "right" : "left"; c.textBaseline = "middle";
        const lx = p.x + (lb.left ? -1 : 1) * ((n.type === "folder" ? r * 1.3 : r) + 5 / t.k);
        if (lb.dy) {                              // a label that moved off its dot's line keeps a thin leader back to it
          c.beginPath(); c.moveTo(p.x, p.y + Math.sign(lb.dy) * r); c.lineTo(lx, p.y + lb.dy / t.k);
          c.lineWidth = 1 / t.k; c.strokeStyle = C.ink3; c.globalAlpha = 0.6; c.stroke(); c.globalAlpha = n.dim ? 0.13 : 1;
        }
        c.fillText(lb.text, lx, p.y + lb.dy / t.k);
      }
    }
    for (const n of this.m.nodes) {
      if (n.type !== "seat" || !show.has(n.id)) continue;
      const p = P.get(n.id);
      c.globalAlpha = n.dim ? 0.13 : 1;
      c.fillStyle = C.ink; c.font = `600 ${12 / t.k}px -apple-system, system-ui, sans-serif`;
      c.textAlign = "center"; c.textBaseline = "middle";
      c.fillText(labelText(n), p.x, p.y);
    }
    c.globalAlpha = 1;
    c.restore();
    this.drawMini(C);
  }

  drawMini(C) {
    const m = this.mctx, P = this.pos, W = 180, H = 120;
    m.clearRect(0, 0, W, H);
    if (!this.m.nodes.length) return;
    const xs = this.m.nodes.map((n) => P.get(n.id).x), ys = this.m.nodes.map((n) => P.get(n.id).y);
    const b = { x0: Math.min(...xs) - 40, x1: Math.max(...xs) + 40, y0: Math.min(...ys) - 40, y1: Math.max(...ys) + 40 };
    const k = Math.min(W / (b.x1 - b.x0), H / (b.y1 - b.y0));
    this.miniMap = { b, k };
    m.fillStyle = C.panel2; m.fillRect(0, 0, W, H);
    for (const n of this.m.nodes) {
      const p = P.get(n.id);
      m.fillStyle = n.needs_you ? C.needs_you : C[n.state] || C.idle;
      if (n.dim) continue;
      m.fillRect((p.x - b.x0) * k - 1.5, (p.y - b.y0) * k - 1.5, 3, 3);
    }
    const t = this.t;
    m.strokeStyle = C.focus; m.lineWidth = 1;
    m.strokeRect((-t.x / t.k - b.x0) * k, (-t.y / t.k - b.y0) * k, (this.W / t.k) * k, (this.H / t.k) * k);
  }

  // Which labels to draw. Seats, needs-you, the hovered and the focused node always; every other label wherever it
  // does not collide with one already placed (screen space, 12 px type at any zoom). Before, labels shrank with the
  // zoom and chat labels were all hidden below zoom 0.95, so a fitted graph read as unlabelled dots.
  labelSet(c, t, P) {
    const must = (n) => n.type === "seat" || n.needs_you || this.hover === n.id || this.focus === n.id || (this.hl && !n.dim);
    const rank = (n) => (n.type === "seat" ? 0 : must(n) ? 1 : n.type === "session" ? 3 : 2);
    const placed = [], show = new Map();               // node id -> { dy: vertical nudge in screen px, left: label sits left of the dot }
    LBL_MAX = this.W < 520 ? 22 : 34;                  // a phone canvas: shorter chat labels
    // Obstacles: every seat box at its drawn size (the box is wider and taller than the seat's label text) and every
    // other drawn shape, so a label never lands on a seat or a dot. A node's own shape is skipped for its own label.
    const shapes = [];
    for (const n of this.m.nodes) {
      if (n.ghost || n.dim) continue;
      const p = P.get(n.id);
      if (!p) continue;
      const sx = p.x * t.k + t.x, sy = p.y * t.k + t.y;
      if (n.type === "seat") {
        c.font = "600 12px -apple-system, system-ui, sans-serif";
        const hw = Math.max(n.r * 1.6 * t.k, (c.measureText(n.label).width + 16) / 2) + 2, hh = Math.max(n.r * 0.8 * t.k, 10) + 2;
        shapes.push({ id: n.id, box: [sx - hw, sy - hh, sx + hw, sy + hh] });
      } else {
        const rr = n.r * t.k + (n.needs_you ? 6 : 1);
        shapes.push({ id: n.id, box: [sx - rr, sy - rr, sx + rr, sy + rr] });
      }
    }
    c.font = "500 12px -apple-system, system-ui, sans-serif";
    for (const n of [...this.m.nodes].sort((a, b) => rank(a) - rank(b))) {
      if (n.ghost || (n.dim && !must(n))) continue;
      const p = P.get(n.id);
      if (!p) continue;
      const sx = p.x * t.k + t.x, sy = p.y * t.k + t.y, rr = n.r * t.k;
      if (n.type === "seat") { show.set(n.id, { dy: 0, left: false, text: labelText(n) }); continue; }   // the seat's own box is the label
      const free = (x, dy) => {
        const b = [x[0], sy - 9 + dy, x[1], sy + 9 + dy];
        return b[1] >= 2 && b[3] <= this.H - 2 && b[0] >= 2 && b[2] <= this.W - 2
          && !shapes.some((s) => s.id !== n.id && b[0] < s.box[2] && b[2] > s.box[0] && b[1] < s.box[3] && b[3] > s.box[1])
          && !placed.some((q) => b[0] < q[2] && b[2] > q[0] && b[1] < q[3] && b[3] > q[1]);
      };
      // plain labels stay on the dot's line to its right; an always-shown label that collides tries the left side of
      // the dot, then moves up or down to the nearest free line, then retries as a short stub; one with no free spot
      // at all is hidden (hover and focus still draw it where it was, on top)
      const full = labelText(n), stub = n.type === "session" ? trunc(n.label, 12) : full;
      let hit = null;
      for (const text of must(n) && stub !== full ? [full, stub] : [full]) {
        const tw = c.measureText(text).width;
        const right = [sx + rr + 4, sx + rr + 8 + tw], left = [sx - rr - 8 - tw, sx - rr - 4];
        const tries = must(n) ? [[right, 0], [left, 0], [right, -15], [right, 15], [left, -15], [left, 15], [right, -30], [right, 30], [left, -30], [left, 30], [right, -45], [right, 45]] : [[right, 0]];
        const f = tries.find(([x, dy]) => free(x, dy));
        if (f) { hit = { x: f[0], dy: f[1], left: f[0] === left, text }; break; }
        if (!hit && (this.hover === n.id || this.focus === n.id)) {   // forced: the spot that covers the least
          const area = (x, dy) => [...shapes.filter((q) => q.id !== n.id).map((q) => q.box), ...placed].reduce((t, q) =>
            t + Math.max(0, Math.min(x[1], q[2]) - Math.max(x[0], q[0])) * Math.max(0, Math.min(sy + 9 + dy, q[3]) - Math.max(sy - 9 + dy, q[1])), 0);
          const best = tries.map(([x, dy]) => ({ x, dy, a: area(x, dy) })).sort((p, q) => p.a - q.a)[0];
          hit = { x: best.x, dy: best.dy, left: best.x === left, text };
        }
      }
      if (!hit) continue;
      show.set(n.id, { dy: hit.dy, left: hit.left, text: hit.text });
      placed.push([hit.x[0], sy - 9 + hit.dy, hit.x[1], sy + 9 + hit.dy]);
    }
    return show;
  }

  fit() {
    if (!this.m) return;
    const P = this.pos, ns = this.m.nodes;
    if (!ns.length || !this.W) return;
    const xs = ns.map((n) => P.get(n.id).tx ?? P.get(n.id).x), ys = ns.map((n) => P.get(n.id).ty ?? P.get(n.id).y);
    const x0 = Math.min(...xs) - 30, x1 = Math.max(...xs) + 30, y0 = Math.min(...ys) - 30, y1 = Math.max(...ys) + 30;
    // Margins in screen pixels (labels are 12 px at any zoom): room for labels on the right, the minimap at the
    // bottom right, and on a phone the bottom navigation bar that floats over the canvas. The left margin holds half a
    // seat box (seat boxes are screen sized, so a leftmost seat used to be cut off by the canvas edge).
    const phone = matchMedia("(max-width: 760px)").matches;
    const padL = 56, padR = Math.min(200, this.W * (phone ? 0.3 : 0.42)), padT = 22;   // a phone's labels may also sit left of their dot
    const padB = phone ? 100 : this.mini && !this.mini.hidden && this.H > 360 ? 140 : 22;
    const aw = Math.max(40, this.W - padL - padR), ah = Math.max(40, this.H - padT - padB);
    const k = Math.max(0.15, Math.min(1.6, Math.min(aw / (x1 - x0), ah / (y1 - y0))));
    this.t = { k, x: padL + (aw - (x1 - x0) * k) / 2 - x0 * k, y: padT + (ah - (y1 - y0) * k) / 2 - y0 * k };
    this.kick();
  }

  // ------------------------------------------------------------------ interaction
  world(ev) { const r = this.canvas.getBoundingClientRect(); return { x: (ev.clientX - r.left - this.t.x) / this.t.k, y: (ev.clientY - r.top - this.t.y) / this.t.k }; }
  hit(w) {
    let best = null, bd = Infinity;
    for (const n of this.m.nodes) {
      const p = this.pos.get(n.id), d = Math.hypot(p.x - w.x, p.y - w.y);
      const reach = (n.type === "seat" ? n.r * 1.7 : n.r) + 6 / this.t.k;
      if (d < reach && d < bd) { best = n; bd = d; }
    }
    return best;
  }

  bind() {
    const cv = this.canvas;
    let press = null;
    cv.addEventListener("pointerdown", (ev) => {
      cv.setPointerCapture(ev.pointerId);
      const w = this.world(ev), n = this.hit(w);
      this.drag = { node: n, sx: ev.clientX, sy: ev.clientY, tx: this.t.x, ty: this.t.y, moved: false };
      this.autoFit = false;
      this.hideMenu();
      if (ev.pointerType === "touch" && n) press = setTimeout(() => { this.drag = null; this.openMenu(n, ev); }, 550);
    });
    cv.addEventListener("pointermove", (ev) => {
      const w = this.world(ev);
      if (this.drag) {
        const dx = ev.clientX - this.drag.sx, dy = ev.clientY - this.drag.sy;
        if (Math.abs(dx) + Math.abs(dy) > 4) { this.drag.moved = true; this.userMoved = true; clearTimeout(press); }
        if (this.drag.node) { const p = this.pos.get(this.drag.node.id); p.x = w.x; p.y = w.y; p.tx = p.ty = null; }
        else { this.t.x = this.drag.tx + dx; this.t.y = this.drag.ty + dy; }
        this.kick();
        return;
      }
      const n = this.hit(w);
      if ((n && n.id) !== this.hover) { this.hover = n ? n.id : null; this.kick(); }
      this.showTip(n, ev);
    });
    cv.addEventListener("pointerup", (ev) => {
      clearTimeout(press);
      const d = this.drag; this.drag = null;
      if (!d) return;
      if (d.node && d.moved) {
        const target = this.hit(this.world(ev));
        const drop = this.m.nodes.find((n) => n.type === "seat" && n !== d.node && Math.hypot(this.pos.get(n.id).x - this.pos.get(d.node.id).x, this.pos.get(n.id).y - this.pos.get(d.node.id).y) < n.r * 2.4);
        if (d.node.type === "work_item" && drop) this.ui.spawn({ workItem: d.node.id.replace("work_item:", ""), seat: drop.seat });
        else if (this.layout === "force") this.pos.get(d.node.id).fixed = true;
        void target;
      } else if (d.node && !d.moved) { this.focus = d.node.id; this.open(d.node); }
      this.kick();
    });
    cv.addEventListener("pointerleave", () => { this.tip.hidden = true; if (this.hover) { this.hover = null; this.kick(); } });
    cv.addEventListener("wheel", (ev) => {
      ev.preventDefault();
      this.autoFit = false; this.userMoved = true;
      const r = cv.getBoundingClientRect(), mx = ev.clientX - r.left, my = ev.clientY - r.top;
      const k = Math.max(0.25, Math.min(3, this.t.k * Math.exp(-ev.deltaY * 0.0015)));
      this.t.x = mx - ((mx - this.t.x) * k) / this.t.k; this.t.y = my - ((my - this.t.y) * k) / this.t.k; this.t.k = k;
      this.kick();
    }, { passive: false });
    cv.addEventListener("contextmenu", (ev) => { ev.preventDefault(); const n = this.hit(this.world(ev)); if (n) this.openMenu(n, ev); });
    cv.addEventListener("keydown", (ev) => this.key(ev));
    this.mini.addEventListener("click", (ev) => {
      if (!this.miniMap) return;
      const r = this.mini.getBoundingClientRect(), { b, k } = this.miniMap;
      const wx = (ev.clientX - r.left) / k + b.x0, wy = (ev.clientY - r.top) / k + b.y0;
      this.t.x = this.W / 2 - wx * this.t.k; this.t.y = this.H / 2 - wy * this.t.k; this.kick();
    });
    this.mini.style.pointerEvents = "auto";
  }

  key(ev) {
    const k = ev.key;
    if (["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(k)) {
      ev.preventDefault();
      const cur = this.focus && this.m.byId.get(this.focus);
      if (!cur) { this.focus = (this.m.nodes.find((n) => n.needs_you) || this.m.nodes[0] || {}).id; this.kick(); this.announce(); return; }
      const p = this.pos.get(cur.id), dir = { ArrowUp: [0, -1], ArrowDown: [0, 1], ArrowLeft: [-1, 0], ArrowRight: [1, 0] }[k];
      let best = null, bs = Infinity;
      for (const n of this.m.nodes) {
        if (n === cur) continue;
        const q = this.pos.get(n.id), dx = q.x - p.x, dy = q.y - p.y, along = dx * dir[0] + dy * dir[1];
        if (along <= 0) continue;
        const s = along + 2.5 * Math.abs(dx * dir[1] - dy * dir[0]);
        if (s < bs) { bs = s; best = n; }
      }
      if (best) { this.focus = best.id; this.ensureVisible(best); this.kick(); this.announce(); }
    } else if (k === "Enter" && this.focus) { ev.preventDefault(); this.open(this.m.byId.get(this.focus)); }
    else if (k === "Escape") { this.closePanel(); this.hideMenu(); }
    else if (k === "+" || k === "=") { this.t.k = Math.min(3, this.t.k * 1.2); this.kick(); }
    else if (k === "-") { this.t.k = Math.max(0.25, this.t.k / 1.2); this.kick(); }
    else if (k === "0") { this.userMoved = false; this.fit(); }
    else if (k === "l") { this.layout = this.layout === "force" ? "lanes" : "force"; this.renderBar(); this.relayout(); }
    else if (k === "v") { this.view = this.view === "tree" ? "work" : "tree"; this.renderBar(); this.relayout(); }
    else if (k === "T") this.toggleTable();
    else if (k === "/") { ev.preventDefault(); this.bar.querySelector(".g-q").focus(); }
    else if (k === "ContextMenu" && this.focus) { const n = this.m.byId.get(this.focus), p = this.pos.get(n.id);
      this.openMenu(n, { clientX: this.canvas.getBoundingClientRect().left + p.x * this.t.k + this.t.x, clientY: this.canvas.getBoundingClientRect().top + p.y * this.t.k + this.t.y }); }
  }
  ensureVisible(n) {
    const p = this.pos.get(n.id), sx = p.x * this.t.k + this.t.x, sy = p.y * this.t.k + this.t.y;
    if (sx < 40 || sx > this.W - 40 || sy < 40 || sy > this.H - 40) { this.t.x = this.W / 2 - p.x * this.t.k; this.t.y = this.H / 2 - p.y * this.t.k; }
  }
  announce() {
    const n = this.m.byId.get(this.focus);
    if (n) this.canvas.setAttribute("aria-label", `${n.type} ${n.label}, ${n.needs_you ? "needs you" : n.state}${n.seat ? ", seat " + n.seat : ""}. Enter opens it.`);
  }

  showTip(n, ev) {
    if (!n) { this.tip.hidden = true; return; }
    const r = this.stage.getBoundingClientRect();
    const rc = n.receipt;
    // native replaceChildren stringifies a false argument: drop the guards that did not hold (the tip read "falsefalsefalse")
    this.tip.replaceChildren(...[
      h("strong", {}, n.label),
      h("div", { class: "hint" }, [n.type, n.needs_you ? "needs you" : n.state, n.seat, n.spend ? `${Math.round(n.spend / 1000)}k units today` : ""].filter(Boolean).join(", ")),
      n.final && h("p", { class: "final" }, n.final.slice(-240)),
      rc && h("div", { class: "receipt" }, h("span", { class: "chip " + (rc.verified ? "risk-low" : "risk-med") }, rc.verified ? "verified" : "not verified"),
        h("span", { class: "chip" }, `${rc.files} files${rc.add != null ? ` +${rc.add} -${rc.del}` : ""}`),
        rc.test && h("span", { class: "testline" }, rc.test)),
      n.type === "cluster" && h("div", { class: "hint" }, "Click to expand"),
      (n.type === "file" || n.type === "folder") && h("div", { class: "hint" }, `${n.path.replace(/^\/Users\/[^/]+/, "~")}, touched by ${(n.sessions || []).length} chats`),
      n.type === "pr" && h("div", { class: "hint" }, n.state.replace("pr_", ""))].filter(Boolean));
    this.tip.hidden = false;
    const x = Math.min(ev.clientX - r.left + 14, r.width - 300), y = Math.min(ev.clientY - r.top + 14, r.height - 160);
    this.tip.style.transform = `translate(${x}px, ${y}px)`;
  }

  // ------------------------------------------------------------------ panel and menu
  open(n) {
    if (!n) return;
    if (n.type === "cluster") { this.expanded.add(n.group); this.relayout(); return; }
    this.tip.hidden = true;
    const ui = this.ui, isS = n.type === "session";
    const ta = isS && !n.excluded && h("textarea", { class: "reply", rows: "3", placeholder: "Reply to this chat", "aria-label": "Reply" });
    const item = isS ? (ui.itemFor(n.session_id) || { kind: "blocked", session_id: n.session_id, key: n.key, title: n.label, excluded: n.excluded }) : null;
    const rc = n.receipt;
    this.panel.replaceChildren(
      h("div", { class: "g-ph" }, h("h3", {}, n.label), h("button", { class: "btn ghost", "aria-label": "Close", onclick: () => this.closePanel() }, "Close")),
      h("div", { class: "who" }, h("span", { class: "chip" }, n.type), n.seat && h("span", { class: "chip" }, n.seat),
        h("span", { class: "chip st-" + (n.needs_you ? "blocked" : n.state) }, n.needs_you ? "needs you" : n.state),
        n.excluded && h("span", { class: "chip ro" }, "read-only")),
      n.activity && h("p", { class: "hint" }, `last activity ${ct(n.activity, true)} (${age(Date.now() / 1000 - n.activity)} ago)`),
      n.final && md(n.final, "final md"),
      rc && h("div", { class: "receipt" }, h("span", { class: "chip " + (rc.verified ? "risk-low" : "risk-med") }, rc.verified ? "verified" : "not verified"),
        h("span", { class: "chip" }, `${rc.files} files`), rc.test && h("span", { class: "testline" }, rc.test),
        (rc.prs || []).map((u) => h("a", { href: u, target: "_blank", rel: "noopener noreferrer" }, u.replace(/^https:\/\/github.com\//, "")))),
      isS && n.needs_you && h("button", { class: "btn primary", onclick: () => ui.jumpTo(n.session_id) }, "Answer on the board"),
      ta && h("div", { class: "replyrow" }, ta, micFor(ta), h("button", { class: "btn", onclick: () => ta.value.trim() && ui.send(item, { text: ta.value.trim() }, ta.value.trim()) }, "Reply")),
      isS && !n.excluded && h("div", { class: "row" },
        h("a", { class: "btn", href: "#/chat/" + encodeURIComponent(n.session_id) }, "Chat history"),
        h("button", { class: "btn", onclick: () => ui.terminal(n) }, "Open terminal"),
        h("button", { class: "btn", onclick: () => ui.stopSession(n) }, "Stop")),
      n.type === "work_item" && h("div", { class: "row" },
        h("button", { class: "btn primary", onclick: () => ui.spawn({ workItem: n.id.replace("work_item:", "") }) }, "New session from this"),
        h("a", { class: "btn", href: `#/work/${encodeURIComponent(n.id.replace("work_item:", ""))}` }, "Open work item")),
      n.type === "seat" && !n.excluded && h("div", { class: "row" },
        h("button", { class: "btn primary", onclick: () => ui.spawn({ seat: n.seat }) }, `New chat on ${n.seat}`)),
      n.type === "repo" && n.path !== "unknown" && h("div", { class: "row" },
        h("button", { class: "btn primary", onclick: () => ui.spawn({ cwd: n.path, title: `New chat in ${n.label}` }) }, "New chat in this repo")),
      (n.type === "file" || n.type === "folder") && this.filePanel(n),
      n.type === "pr" && h("div", {}, h("p", {}, h("a", { href: n.url, target: "_blank", rel: "noopener noreferrer" }, n.url.replace(/^https:\/\/github.com\//, ""))),
        h("p", { class: "hint" }, `state: ${n.state.replace("pr_", "")}${n.state === "pr_unknown" ? " (gh has not been asked yet; it is read in the background)" : ""}`),
        this.sessionList(n.sessions),
        h("button", { class: "btn", onclick: () => ui.spawn({ brief: `Follow up on ${n.url}: `, title: `New chat about ${n.label}` }) }, "New chat about this PR")),
      (isS || n.type === "work_item" || (n.type === "repo" && n.path !== "unknown")) && h("details", { class: "evidence g-files", ontoggle: (e) => {
        if (!e.target.open || e.target.dataset.loaded) return;
        e.target.dataset.loaded = "1";
        const params = isS ? { session: n.session_id } : n.type === "work_item" ? { work_item: n.id.replace("work_item:", "") } : { repo: n.path };
        filesBrowser(e.target.querySelector(".fb"), params, this, n.type === "repo" ? n.path : null);
      } }, h("summary", {}, "Files touched (one folder level at a time)"), h("div", { class: "fb" })));
    this.panel.hidden = false;
    (ta || this.panel.querySelector("button")).focus();
  }
  closePanel() { this.panel.hidden = true; this.canvas.focus(); }

  sessionList(sids) {
    const by = new Map((this.data?.nodes || []).filter((x) => x.type === "session").map((x) => [x.session_id, x]));
    const known = (sids || []).map((sid) => by.get(sid)).filter(Boolean);
    const other = (sids || []).length - known.length;
    return h("div", { class: "g-sl" }, h("div", { class: "hint" }, `Touched by ${(sids || []).length} chat${(sids || []).length === 1 ? "" : "s"}`),
      h("ul", { class: "sess" }, known.map((x) => h("li", {}, h("span", { class: "st " + (x.needs_you ? "needs_you" : x.state), "aria-hidden": "true" }),
        h("a", { class: "nm", href: "#/chat/" + encodeURIComponent(x.session_id) }, x.label), h("span", { class: "rt" }, x.seat)))),
      other > 0 && h("p", { class: "hint" }, `${other} older chat${other === 1 ? "" : "s"} not in view`));
  }

  filePanel(n) {
    const ui = this.ui, folder = n.type === "folder";
    const dir = folder ? n.path : n.path.split("/").slice(0, -1).join("/");
    const box = h("div", { class: "fb" });
    return h("div", { class: "g-filep" },
      h("p", { class: "hint mono" }, n.path.replace(/^\/Users\/[^/]+/, "~")),
      n.hot && h("p", { class: "receipt none" }, `Written by ${n.n_sessions} chats in the window: a likely overlap.`),
      this.sessionList(n.sessions),
      h("div", { class: "row" },
        h("button", { class: "btn primary", onclick: () => ui.spawn({ cwd: dir, title: `New chat about ${n.label}`,
          brief: folder ? `Work in ${n.path}: ` : `About ${n.path}: ` }) }, "New chat about this"),
        this.pins.has(n.path) ? h("button", { class: "btn ghost", onclick: () => { this.unpin(n.path); this.closePanel(); } }, "Unpin")
          : h("button", { class: "btn ghost", onclick: () => this.pin({ path: n.path, label: n.label, dir: folder, sessions: n.sessions }) }, "Pin")),
      folder && h("details", { class: "evidence", ontoggle: (e) => { if (e.target.open && !box.firstChild) filesBrowser(box, {}, this, n.path); } },
        h("summary", {}, "Inside this folder"), box));
  }

  openMenu(n, ev) {
    const r = this.stage.getBoundingClientRect(), ui = this.ui;
    const wi = n.type === "work_item" ? n.id.replace("work_item:", "") : (n.parent || "").startsWith("work_item:") ? n.parent.replace("work_item:", "") : null;
    const item = (label, fn, disabled) => h("button", { role: "menuitem", disabled: !!disabled, onclick: () => { this.hideMenu(); fn(); } }, label);
    const seats = (this.data.nodes || []).filter((x) => x.type === "seat" && !x.excluded);
    this.menu.replaceChildren(
      h("div", { class: "hint" }, n.label),
      item("Spawn a child session here", () => ui.spawn({ workItem: wi, seat: n.seat, parent: n.type === "session" ? n.session_id : null }), n.excluded),
      h("div", { class: "hint" }, "Assign to seat"),
      ...seats.map((s) => item(`  ${s.seat} (${s.state})`, () => ui.spawn({ workItem: wi, seat: s.seat }), !wi)),
      n.type === "session" && item("Mark done (propose handoff)", () => ui.markDone(n), n.excluded),
      n.type === "seat" && item(`New chat on ${n.seat}`, () => ui.spawn({ seat: n.seat }), n.excluded),
      n.type === "repo" && item("New chat in this repo", () => ui.spawn({ cwd: n.path, title: `New chat in ${n.label}` }), n.path === "unknown"),
      (n.type === "file" || n.type === "folder") && item("New chat about this", () => ui.spawn({ cwd: n.type === "folder" ? n.path : n.path.split("/").slice(0, -1).join("/"), brief: `About ${n.path}: ` })));
    this.menu.hidden = false;
    this.menu.style.transform = `translate(${Math.min(ev.clientX - r.left, r.width - 240)}px, ${Math.min(ev.clientY - r.top, r.height - 300)}px)`;
    this.menu.querySelector("button:not([disabled])")?.focus();
  }
  hideMenu() { this.menu.hidden = true; }

  toggleTable() {
    this.table.hidden = !this.table.hidden; if (!this.table.hidden) this.renderTable(); this.renderBar();
    // on a phone the table opens below the map, off screen: bring it up so the button visibly does something
    if (!this.table.hidden && matchMedia("(max-width: 760px)").matches) requestAnimationFrame(() => this.table.scrollIntoView({ block: "start", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" }));
  }
  renderTable() {
    const rows = this.m.nodes.slice().sort((a, b) => rank(a) - rank(b));
    this.table.replaceChildren(h("table", {}, h("caption", {}, `${rows.length} nodes (${this.view === "tree" ? "agent tree" : "work graph"})`),
      h("thead", {}, h("tr", {}, ["Type", "Name", "State", "Seat", "Spend today", "Links"].map((x) => h("th", { scope: "col" }, x)))),
      h("tbody", {}, rows.map((n) => h("tr", {}, h("td", {}, n.type), h("td", {}, h("button", { class: "linkish", onclick: () => this.open(n) }, n.label)),
        h("td", {}, n.needs_you ? "needs you" : n.state), h("td", {}, n.seat || ""), h("td", { class: "num" }, n.spend ? Math.round(n.spend).toLocaleString() : ""),
        h("td", {}, this.m.edges.filter((e) => e.from === n.id).map((e) => `${e.kind} ${(this.m.byId.get(e.to) || {}).label || e.to}`).join("; ")))))));
  }
}

function radius(n, max) {
  if (n.type === "seat") return 16;
  if (n.type === "repo") return 12;
  if (n.type === "file") return 5 + Math.min(4, (n.n_sessions || 1) - 1);
  if (n.type === "folder") return 8;
  if (n.type === "pr") return 7;
  if (n.type === "work_item") return 11;
  if (n.type === "job") return 4;
  if (n.type === "cluster") return 9 + Math.min(10, n.members.length);
  return 5 + 11 * Math.sqrt((n.spend || 0) / max);
}
function rank(n) { return n.needs_you ? 0 : n.state === "working" ? 1 : n.type === "cluster" ? 3 : n.state === "idle" ? 2 : 4; }
let LBL_MAX = 34;
function labelText(n) {
  const base = trunc(n.type === "work_item" ? n.label.split(" - ")[0] : n.label, n.type === "session" ? LBL_MAX : 40);
  return base + (n.type === "folder" && n.files ? ` (${n.files})` : n.type === "repo" ? ` (${n.members})` : "");
}
function trunc(s, n) { s = String(s || ""); return s.length > n ? s.slice(0, n - 1) + "..." : s; }
function roundRect(c, x, y, w, hh, r) { c.moveTo(x + r, y); c.arcTo(x + w, y, x + w, y + hh, r); c.arcTo(x + w, y + hh, x, y + hh, r); c.arcTo(x, y + hh, x, y, r); c.arcTo(x, y, x + w, y, r); c.closePath(); }
export { toast };
