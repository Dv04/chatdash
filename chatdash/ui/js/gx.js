// Graph page views beyond the map: hotspots treemap, spend flow, cost per outcome, waiting
// chain, overlap detector, PRs, plus the folder browser the map's panel uses. Every view reads /api/cp/gx/*;
// nothing here acts except "New chat", which opens the usual spawn dialog. Each chart has a table view.
import { h, ct, age, toast } from "./lib.js";
import { get } from "./api.js";

export const MODES = [["map", "map"], ["hotspots", "hotspots"], ["spend", "spend"], ["outcomes", "cost per outcome"],
  ["waiting", "waiting"], ["overlaps", "overlaps"], ["prs", "PRs"]];
const SVGNS = "http://www.w3.org/2000/svg";
const k = (n) => (n == null ? "?" : n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? Math.round(n / 1e3) + "k" : String(Math.round(n)));
const tilde = (p) => (p || "").replace(/^\/Users\/[^/]+/, "~");
function svg(tag, attrs = {}, ...kids) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [a, v] of Object.entries(attrs)) if (v != null && v !== false) el.setAttribute(a, v);
  for (const c of kids.flat()) if (c != null && c !== false) el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return el;
}
function seg(label, opts, cur, set) {
  return h("div", { class: "seg", role: "group", "aria-label": label },
    opts.map(([v, l]) => h("button", { "aria-pressed": String(String(cur) === String(v)), onclick: () => set(v) }, l)));
}
function store(g, key, def) { return g.ui.store.get("gx." + key, def); }
function save(g, key, v) { g.ui.store.set("gx." + key, v); }
function tip(el) {
  let t = el.querySelector(".gx-tip");
  if (!t) { t = h("div", { class: "g-tip gx-tip", role: "tooltip", hidden: true }); el.append(t); }
  return {
    show(ev, ...kids) { const r = el.getBoundingClientRect(); t.replaceChildren(...kids); t.hidden = false;
      t.style.transform = `translate(${Math.min(ev.clientX - r.left + 14, r.width - 280)}px, ${Math.min(ev.clientY - r.top + 14, r.height - 120)}px)`; },
    hide() { t.hidden = true; },
  };
}
function head(title, note, ...controls) {
  return h("div", { class: "gx-head" }, h("div", {}, h("h2", {}, title), note && h("p", { class: "hint" }, note)), h("div", { class: "gx-ctl" }, controls));
}
function tableToggle(g, key) {
  const on = store(g, key + ".table", false);
  return h("button", { class: "btn ghost", "aria-pressed": String(on), onclick: () => { save(g, key + ".table", !on); renderMode(g.vw, g.mode, g); } }, "Table");
}
function fail(el, title, e) { el.replaceChildren(h("div", { class: "empty" }, h("h3", {}, title), h("p", {}, e.message || String(e)))); }
const sessLink = (s) => h("a", { href: "#/chat/" + encodeURIComponent(s.session_id), class: "nm" }, s.name || s.title || s.session_id.slice(0, 8));

export async function renderMode(el, mode, g) {
  const fn = { hotspots, spend, outcomes, waiting, overlaps, prs }[mode];
  if (!fn) return;
  if (!el.firstChild || el.dataset.mode !== mode) el.replaceChildren(h("div", { class: "skeleton" }));
  el.dataset.mode = mode;
  try { await fn(el, g); } catch (e) { fail(el, mode, e); }
}

// ------------------------------------------------------------------ folder browser (map panel)
export async function filesBrowser(el, params, g, prefix = null, trail = []) {
  el.replaceChildren(h("div", { class: "hint" }, "Loading"));
  const qs = new URLSearchParams({ ...params, ...(prefix ? { prefix } : {}) });
  let d;
  try { d = await get("gx/files?" + qs); } catch (e) { el.replaceChildren(h("p", { class: "receipt none" }, e.message)); return; }
  const crumbs = h("div", { class: "fb-crumbs" },
    h("button", { class: "linkish", onclick: () => filesBrowser(el, params, g, null, []) }, "all"),
    trail.map((t, i) => [" / ", h("button", { class: "linkish", onclick: () => filesBrowser(el, params, g, t, trail.slice(0, i + 1)) }, t.split("/").pop())]));
  const rows = d.items.map((it) => h("li", {},
    it.dir ? h("button", { class: "linkish", onclick: () => filesBrowser(el, params, g, it.path, trail.concat(it.path)) }, (prefix ? it.label : tilde(it.label)) + "/")
      : h("span", { class: "mono" }, it.label),
    h("span", { class: "rt" }, `${it.dir ? it.files + " files, " : ""}${it.w} edits, ${it.sessions.length} chat${it.sessions.length === 1 ? "" : "s"}`),
    h("button", { class: "btn ghost sm", title: "Show on the map, linked to every chat that touched it", onclick: () => g.pin({ path: it.path, label: it.dir ? (prefix ? it.label : tilde(it.label)) : it.label, dir: it.dir, sessions: it.sessions, files: it.files }) }, "Pin")));
  el.replaceChildren(crumbs, rows.length ? h("ul", { class: "fb-list" }, rows) : h("p", { class: "hint" }, "No files touched here"),
    d.total > d.items.length && h("p", { class: "hint" }, `${d.total - d.items.length} more not shown`),
    h("p", { class: "hint" }, "From Edit, Write and Read calls; files changed only through shell commands are not seen."));
}

// ------------------------------------------------------------------ hotspots (squarified treemap)
async function hotspots(el, g) {
  const win = store(g, "hs.window", 7 * 86400), metric = store(g, "hs.metric", "edits");
  const d = await get("gx/hotspots" + (win ? `?window=${win}` : "?window=0"));
  const root = { name: "all", path: "", children: new Map(), edits: 0, sessions: 0, last: 0, files: 0 };
  for (const f of d.files) {
    const rel = f.path.startsWith(f.repo + "/") ? f.path.slice(f.repo.length + 1) : f.path;
    const parts = [tilde(f.repo), ...rel.split("/")];
    let node = root;
    parts.forEach((p, i) => {
      const path = i === 0 ? f.repo : node.path + "/" + p;
      if (!node.children.has(p)) node.children.set(p, { name: p, path, children: new Map(), edits: 0, sessions: 0, last: 0, files: 0, file: i === parts.length - 1 });
      node = node.children.get(p);
    });
    for (let n = root, i = 0; i <= parts.length; i++) {
      n.edits += f.edits; n.sessions = Math.max(n.sessions, f.sessions); n.last = Math.max(n.last, f.last_at || 0); n.files += 1;
      if (i < parts.length) n = n.children.get(parts[i]);
    }
  }
  let cur = root;
  for (const p of store(g, "hs.drill", [])) { const nx = cur.children.get(p); if (!nx) break; cur = nx; }
  const drill = (names) => { save(g, "hs.drill", names); renderMode(el, "hotspots", g); };
  const names = store(g, "hs.drill", []).slice(0, depthOf(root, store(g, "hs.drill", [])));
  const crumbs = h("div", { class: "fb-crumbs" }, h("button", { class: "linkish", onclick: () => drill([]) }, "all repos"),
    names.map((n, i) => [" / ", h("button", { class: "linkish", onclick: () => drill(names.slice(0, i + 1)) }, n)]));
  const kids = [...cur.children.values()].map((c) => ({ ...c, value: metric === "edits" ? c.edits : c.sessions * 10 + c.files }))
    .filter((c) => c.value > 0).sort((a, b) => b.value - a.value).slice(0, 120);
  const now = Date.now() / 1000;
  const rec = (t) => { const a = now - t; return a < 3600 ? 4 : a < 86400 ? 3 : a < 3 * 86400 ? 2 : a < 7 * 86400 ? 1 : 0; };
  const controls = [
    seg("Window", [[86400, "24 h"], [7 * 86400, "7 days"], [30 * 86400, "30 days"], [0, "all"]], win, (v) => { save(g, "hs.window", Number(v)); save(g, "hs.drill", []); renderMode(el, "hotspots", g); }),
    seg("Size", [["edits", "by edits"], ["chats", "by files and chats"]], metric, (v) => { save(g, "hs.metric", v); renderMode(el, "hotspots", g); }),
    tableToggle(g, "hs")];
  const top = head("Hotspots", `Where the chats' edits go: area = ${metric === "edits" ? "edit calls" : "files and chats"}, color = how recently. ${d.files.length} files in the window. Click a box to go inside.`, ...controls);
  if (!kids.length) { el.replaceChildren(top, crumbs, h("div", { class: "empty" }, h("h3", {}, "No edits in this window"))); return; }
  if (store(g, "hs.table", false)) {
    el.replaceChildren(top, crumbs, h("div", { class: "table-wrap" }, h("table", { class: "sess-table" },
      h("thead", {}, h("tr", {}, ["Name", "Edits", "Files", "Max chats per file", "Last edit"].map((x) => h("th", {}, x)))),
      h("tbody", {}, kids.map((c) => h("tr", {}, h("td", { class: "nm" }, c.file ? c.name : h("button", { class: "linkish", onclick: () => drill(names.concat(c.name)) }, c.name + "/")),
        h("td", { class: "num" }, c.edits), h("td", { class: "num" }, c.files), h("td", { class: "num" }, c.sessions), h("td", {}, ct(c.last, true))))))));
    return;
  }
  const W = Math.max(320, el.clientWidth - 2), H = Math.max(360, Math.min(720, window.innerHeight - 280));
  const rects = squarify(kids, 0, 0, W, H);
  const t = tip(el);
  const box = svg("svg", { class: "gx-tree", viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img", "aria-label": `Treemap of ${kids.length} items` },
    rects.map((r) => {
      const c = r.item, lvl = rec(c.last);
      const gEl = svg("g", { class: `tm l${lvl}${c.file ? " file" : ""}`, tabindex: "0" },
        svg("rect", { x: r.x + 1, y: r.y + 1, width: Math.max(0, r.w - 2), height: Math.max(0, r.h - 2), rx: 3 }),
        r.w > 46 && r.h > 18 && svg("text", { x: r.x + 6, y: r.y + 15 }, trunc(c.name, Math.floor(r.w / 7))),
        r.w > 46 && r.h > 34 && svg("text", { x: r.x + 6, y: r.y + 30, class: "sub" }, `${k(c.edits)} edits${c.file ? "" : `, ${c.files} files`}`));
      const open = () => c.file ? fileCard(el, g, c) : drill(names.concat(c.name));
      gEl.addEventListener("click", open);
      gEl.addEventListener("keydown", (e) => { if (e.key === "Enter") open(); });
      gEl.addEventListener("pointermove", (e) => t.show(e, h("strong", {}, c.name + (c.file ? "" : "/")),
        h("div", { class: "hint" }, `${c.edits} edits, ${c.files} files, up to ${c.sessions} chats on one file, last ${ct(c.last, true)}`)));
      gEl.addEventListener("pointerleave", () => t.hide());
      return gEl;
    }));
  const legend = h("div", { class: "gx-legend" }, h("span", {}, "last edit:"),
    ["over a week", "this week", "last 3 days", "today", "last hour"].map((l, i) => h("span", { class: "sw" }, h("i", { class: "tm-sw l" + i }), l)));
  el.replaceChildren(top, crumbs, h("div", { class: "gx-chart" }, box), legend);
}
function depthOf(root, names) { let n = root, i = 0; for (const p of names) { if (!n.children.has(p)) break; n = n.children.get(p); i++; } return i; }
async function fileCard(el, g, c) {
  const r = await get("gx/search?q=" + encodeURIComponent(c.path)).catch(() => null);
  const f = r && r.files.find((x) => x.path === c.path);
  const sids = f ? f.sessions : [];
  const card = h("div", { class: "pop gx-card", role: "dialog", "aria-label": c.name },
    h("h3", {}, c.name), h("p", { class: "hint mono" }, tilde(c.path)), g.sessionList(sids),
    h("div", { class: "row" },
      h("button", { class: "btn primary", onclick: () => { card.remove(); g.ui.spawn({ cwd: c.path.split("/").slice(0, -1).join("/"), brief: `About ${c.path}: `, title: `New chat about ${c.name}` }); } }, "New chat about this"),
      h("button", { class: "btn", onclick: () => { g.pin({ path: c.path, label: c.name, dir: false, sessions: sids }); card.remove(); } }, "Pin to map"),
      h("button", { class: "btn ghost", onclick: () => card.remove() }, "Close")));
  document.querySelectorAll(".gx-card").forEach((x) => x.remove());
  document.body.append(card);
  card.querySelector("button").focus();
}
// Squarified treemap (Bruls, Huizing, van Wijk): rows that keep tiles close to square.
function squarify(items, x, y, w, hgt) {
  const total = items.reduce((t, i) => t + i.value, 0) || 1;
  const area = items.map((i) => ({ item: i, a: (i.value / total) * w * hgt }));
  const out = [];
  let rx = x, ry = y, rw = w, rh = hgt, row = [];
  const worst = (r, side) => { const s = r.reduce((t, i) => t + i.a, 0), mx = Math.max(...r.map((i) => i.a)), mn = Math.min(...r.map((i) => i.a));
    return Math.max((side * side * mx) / (s * s), (s * s) / (side * side * mn)); };
  const lay = (r) => {
    const s = r.reduce((t, i) => t + i.a, 0);
    if (rw >= rh) { const cw = s / rh; let cy = ry; for (const i of r) { const ch = i.a / cw; out.push({ item: i.item, x: rx, y: cy, w: cw, h: ch }); cy += ch; } rx += cw; rw -= cw; }
    else { const ch = s / rw; let cx = rx; for (const i of r) { const cw = i.a / ch; out.push({ item: i.item, x: cx, y: ry, w: cw, h: ch }); cx += cw; } ry += ch; rh -= ch; }
  };
  for (const i of area) {
    const side = Math.min(rw, rh);
    if (!row.length || worst(row.concat(i), side) <= worst(row, side)) row.push(i);
    else { lay(row); row = [i]; }
  }
  if (row.length) lay(row);
  return out;
}
function trunc(s, n) { s = String(s || ""); return n < 2 ? "" : s.length > n ? s.slice(0, Math.max(1, n - 1)) + "..." : s; }

// ------------------------------------------------------------------ spend flow (seat -> group -> chat)
async function spend(el, g) {
  const days = store(g, "sp.days", 1), group = store(g, "sp.group", "work_item");
  const d = await get(`gx/spend?days=${days}&group=${group}`);
  const controls = [seg("Window", [[1, "today (UTC)"], [7, "7 days"]], days, (v) => { save(g, "sp.days", Number(v)); renderMode(el, "spend", g); }),
    seg("Middle", [["work_item", "work item"], ["repo", "repo"]], group, (v) => { save(g, "sp.group", v); renderMode(el, "spend", g); }), tableToggle(g, "sp")];
  const top = head("Spend flow", `Limit units ${days === 1 ? "today" : "over 7 days"}: ${k(d.total)} in all. Band width = units. Seat gauges show where each seat stands now.`, ...controls);
  if (!d.rows.length) { el.replaceChildren(top, h("div", { class: "empty" }, h("h3", {}, "No spend in this window"))); return; }
  if (store(g, "sp.table", false)) {
    el.replaceChildren(top, h("div", { class: "table-wrap" }, h("table", { class: "sess-table" },
      h("thead", {}, h("tr", {}, ["Seat", group === "repo" ? "Repo" : "Work item", "Chat", "Units"].map((x) => h("th", {}, x)))),
      h("tbody", {}, d.rows.map((r) => h("tr", {}, h("td", {}, r.seat), h("td", {}, r.group), h("td", { class: "nm" }, r.session_id ? sessLink(r) : r.name), h("td", { class: "num" }, k(r.units))))))));
    return;
  }
  const cols = [[], [], []];
  const add = (ci, key, label, u, extra) => { let n = cols[ci].find((x) => x.key === key); if (!n) { n = { key, label, u: 0, ...extra }; cols[ci].push(n); } n.u += u; return n; };
  const links = [];
  const seatInfo = new Map(d.seats.map((s) => [s.seat, s]));
  for (const r of d.rows) {
    const a = add(0, r.seat, r.seat, r.units, { seat: seatInfo.get(r.seat) }), b = add(1, r.group, r.group, r.units), c = add(2, r.key || r.name + r.seat, r.name, r.units, { row: r });
    links.push({ a, b, u: r.units }, { a: b, b: c, u: r.units });
  }
  for (const col of cols) col.sort((x, y) => y.u - x.u);
  const W = Math.max(640, el.clientWidth - 2), gap = 6, colX = [10, W * 0.36, W * 0.66], nodeW = 12;
  const H = Math.max(360, Math.max(...cols.map((c) => c.length)) * 26);
  const scale = (H - gap * 30) / d.rows.reduce((t, r) => t + r.units, 0);
  cols.forEach((col, ci) => { let y = 0; for (const n of col) { n.x = colX[ci]; n.y = y; n.h = Math.max(2, n.u * scale); n.out = 0; n.in = 0; y += n.h + gap; } });
  const merged = new Map();
  for (const l of links) { const key = l.a.key + "|" + l.b.key + "|" + (l.a.x); const m = merged.get(key); if (m) m.u += l.u; else merged.set(key, { ...l }); }
  const t = tip(el);
  const paths = [...merged.values()].sort((p, q) => q.u - p.u).map((l) => {
    const th = Math.max(1, l.u * scale), y0 = l.a.y + l.a.out + th / 2, y1 = l.b.y + l.b.in + th / 2;
    l.a.out += th; l.b.in += th;
    const x0 = l.a.x + nodeW, x1 = l.b.x, mx = (x0 + x1) / 2;
    const p = svg("path", { d: `M${x0},${y0} C${mx},${y0} ${mx},${y1} ${x1},${y1}`, "stroke-width": th, class: "sk-link" });
    p.addEventListener("pointermove", (e) => t.show(e, h("strong", {}, `${l.a.label} to ${l.b.label}`), h("div", { class: "hint" }, `${k(l.u)} units`)));
    p.addEventListener("pointerleave", () => t.hide());
    return p;
  });
  const nodesEl = cols.flat().map((n) => {
    const full = n.seat && (n.seat.five >= 100 || n.seat.seven >= 100), near = n.seat && (n.seat.five >= 80);
    const gEl = svg("g", { class: "sk-node" + (full ? " full" : near ? " near" : "") },
      svg("rect", { x: n.x, y: n.y, width: nodeW, height: n.h, rx: 2 }),
      svg("text", { x: n.x === colX[2] ? n.x + nodeW + 6 : n.x + nodeW + 6, y: n.y + Math.min(n.h / 2, 12) + 4 },
        `${trunc(n.label, n.x === colX[2] ? 46 : 24)}  ${k(n.u)}${n.seat ? `  (5h ${n.seat.five ?? "?"}%)` : ""}`));
    if (n.row && n.row.session_id) gEl.addEventListener("click", () => { location.hash = "#/chat/" + encodeURIComponent(n.row.session_id); });
    return gEl;
  });
  el.replaceChildren(top, h("div", { class: "gx-chart" }, svg("svg", { class: "gx-sankey", viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img",
    "aria-label": `Spend from ${cols[0].length} seats through ${cols[1].length} groups to ${cols[2].length} chats` }, paths, nodesEl)),
    h("div", { class: "gx-legend" }, h("span", { class: "sw" }, h("i", { class: "sk-sw full" }), "seat at its limit"), h("span", { class: "sw" }, h("i", { class: "sk-sw near" }), "seat 5h at 80% or more"),
      h("span", {}, "The top 24 chats are shown; the rest fold into one row per seat.")));
}

// ------------------------------------------------------------------ cost per outcome
async function outcomes(el, g) {
  const days = store(g, "oc.days", 7), dim = store(g, "oc.dim", "work_item");
  const d = await get(`gx/outcomes?days=${days}`);
  const rows = d.groups.filter((x) => x.dim === dim);
  const controls = [seg("Window", [[1, "today"], [7, "7 days"]], days, (v) => { save(g, "oc.days", Number(v)); renderMode(el, "outcomes", g); }),
    seg("By", [["work_item", "work item"], ["seat", "seat"], ["repo", "repo"]], dim, (v) => { save(g, "oc.dim", v); renderMode(el, "outcomes", g); })];
  const top = head("Cost per outcome", d.note + (d.pr_states_pending ? `. ${d.pr_states_pending} PR states are still being read from gh; numbers fill in as they arrive.` : ""), ...controls);
  const withPr = rows.filter((r) => r.per_merged != null).sort((a, b) => b.per_merged - a.per_merged);
  const max = Math.max(1, ...withPr.map((r) => r.per_merged));
  const bars = withPr.length ? h("div", { class: "gx-bars", role: "list", "aria-label": "Units per merged PR" },
    withPr.map((r) => h("div", { class: "gx-bar", role: "listitem" }, h("span", { class: "lbl" }, r.key),
      h("span", { class: "track" }, h("i", { style: `width:${(100 * r.per_merged) / max}%` })), h("span", { class: "val" }, `${k(r.per_merged)} per merged PR`)))) :
    h("p", { class: "hint" }, "No merged PRs linked from these chats in the window (or gh has not reported them yet).");
  const table = h("div", { class: "table-wrap" }, h("table", { class: "sess-table" },
    h("thead", {}, h("tr", {}, [dim === "work_item" ? "Work item" : dim === "seat" ? "Seat" : "Repo", "Units", "Chats", "Merged PRs", "Units per merged PR", "Verified turns", "Units per verified turn"].map((x) => h("th", {}, x)))),
    h("tbody", {}, rows.map((r) => h("tr", {}, h("td", { class: "nm" }, r.key), h("td", { class: "num" }, k(r.units)), h("td", { class: "num" }, r.sessions),
      h("td", { class: "num" }, r.merged), h("td", { class: "num" }, r.per_merged == null ? h("span", { class: "hint" }, r.units ? "no merged PR" : "") : k(r.per_merged)),
      h("td", { class: "num" }, r.verified), h("td", { class: "num" }, r.per_verified == null ? "" : k(r.per_verified)))))));
  el.replaceChildren(top, h("h3", { class: "gx-sub" }, "Units per merged PR (higher = more spend per shipped change)"), bars, h("h3", { class: "gx-sub" }, "All groups"), table);
}

// ------------------------------------------------------------------ waiting chain
async function waiting(el, g) {
  const d = await get("gx/waiting");
  const top = head("Waiting chain", d.longest ? `Longest wait: ${d.longest.title}, ${age(d.longest.seconds)}.` : "Nothing is waiting.");
  if (!d.chains.length) { el.replaceChildren(top, h("div", { class: "empty" }, h("h3", {}, "Nothing waits"))); return; }
  el.replaceChildren(top, h("div", { class: "gx-chains" }, d.chains.map((ch) => {
    const max = Math.max(1, ...ch.items.map((i) => i.seconds || 0));
    return h("section", { class: "group" },
      h("h3", {}, ch.label, ch.resets_at && h("span", { class: "hint" }, ` resets ${ct(ch.resets_at)}`), h("span", { class: "n" }, String(ch.items.length))),
      h("ul", { class: "gx-wait" }, ch.items.map((i) => h("li", { class: d.longest && i.session_id === d.longest.session_id && i.seconds === d.longest.seconds ? "longest" : "" },
        h("span", { class: "track" }, h("i", { style: `width:${(100 * (i.seconds || 0)) / max}%` })),
        h("a", { class: "nm", href: "#/chat/" + encodeURIComponent(i.session_id) }, i.title),
        h("span", { class: "chip" }, i.kind), i.running === false && h("span", { class: "chip ro" }, "stopped"),
        i.passed && h("span", { class: "chip risk-med" }, "reset passed"),
        h("span", { class: "rt" }, i.seconds != null ? age(i.seconds) : "?")))));
  })), h("p", { class: "hint" }, "Answer questions on the board; limit stalls clear at the reset (or by limit resume); queued replies go in when the chat finishes its turn."));
}

// ------------------------------------------------------------------ overlap detector
async function overlaps(el, g) {
  const win = store(g, "ov.window", 86400);
  const d = await get("gx/overlaps?window=" + win);
  const controls = [seg("Window", [[7200, "2 h"], [86400, "24 h"], [7 * 86400, "7 days"]], win, (v) => { save(g, "ov.window", Number(v)); renderMode(el, "overlaps", g); })];
  const top = head("Overlap detector", "Pairs of chats that wrote the same files in the window. High = both running and one is working right now: they can overwrite each other.", ...controls);
  if (!d.overlaps.length) { el.replaceChildren(top, h("div", { class: "empty" }, h("h3", {}, "No overlaps in this window"))); return; }
  el.replaceChildren(top, h("ol", { class: "strips" }, d.overlaps.slice(0, 60).map((o) => h("li", { class: "strip gx-ov" },
    h("div", { class: "wait" }, h("span", { class: "age" }, String(o.shared)), h("span", { class: "unit" }, o.shared === 1 ? "file" : "files"), h("span", { class: "kind" }, o.risk + " risk")),
    h("div", { class: "body" },
      h("div", { class: "who" }, sessLink(o.a), h("span", { class: "chip" }, o.a.seat), h("span", { class: "chip" }, o.a.live ? o.a.state : "stopped"), " and ",
        sessLink(o.b), h("span", { class: "chip" }, o.b.seat), h("span", { class: "chip" }, o.b.live ? o.b.state : "stopped"),
        h("span", { class: "chip risk-" + o.risk }, o.risk)),
      h("details", { class: "evidence" }, h("summary", {}, `Shared files, last overlap ${ct(o.last_at, true)}`),
        h("ul", { class: "fb-list" }, o.files.map((f) => h("li", { class: "mono" }, f))), o.shared > o.files.length && h("p", { class: "hint" }, `${o.shared - o.files.length} more`)))))));
}

// ------------------------------------------------------------------ PRs
async function prs(el, g) {
  const st = store(g, "pr.state", "all");
  const d = await get("gx/prs");
  const by = new Map();
  for (const p of d.prs) { if (!by.has(p.url)) by.set(p.url, { ...p, sessions: [] }); by.get(p.url).sessions.push(p.session_id); }
  const names = new Map((g.data?.nodes || []).filter((n) => n.type === "session").map((n) => [n.session_id, n.label]));
  const all = [...by.values()];
  const count = (s) => all.filter((p) => p.state === s).length;
  const order = { open: 0, unknown: 1, merged: 2, closed: 3 };
  const list = all.filter((p) => st === "all" || p.state === st).sort((a, b) => (order[a.state] ?? 4) - (order[b.state] ?? 4) || b.num - a.num);
  const controls = [seg("State", [["all", `all ${all.length}`], ["open", `open ${count("open")}`], ["merged", `merged ${count("merged")}`], ["closed", `closed ${count("closed")}`], ["unknown", `unknown ${count("unknown")}`]], st,
    (v) => { save(g, "pr.state", v); renderMode(el, "prs", g); })];
  const top = head("PRs", "PRs linked from the chats in view. State comes from gh (cached 15 min, read in the background, so new ones show unknown first). The map's PR layer draws them as hexagons.", ...controls);
  el.replaceChildren(top, h("div", { class: "table-wrap" }, h("table", { class: "sess-table" },
    h("thead", {}, h("tr", {}, ["PR", "Repo", "State", "Chats"].map((x) => h("th", {}, x)))),
    h("tbody", {}, list.map((p) => h("tr", {}, h("td", {}, h("a", { href: p.url, target: "_blank", rel: "noopener noreferrer" }, "#" + p.num)), h("td", {}, p.repo),
      h("td", {}, h("span", { class: "chip pr-" + p.state }, p.state)),
      h("td", { class: "nm" }, p.sessions.map((sid, i) => [i ? ", " : "", h("a", { href: "#/chat/" + encodeURIComponent(sid) }, names.get(sid) || sid.slice(0, 8))]))))))));
}
export { toast };
