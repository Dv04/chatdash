// Nebula look, stage 1: the board only, behind a per-browser toggle (Settings "Look", or ?look=nebula).
// The current look stays the default. Every board function is the same DOM, restyled into three glass windows
// (css/nebula.css) over the live field (js/field.js). Other views are untouched: the look applies on #/ only.
import { h, store, toast } from "./lib.js";
import { Field, fieldModel } from "./field.js";
import { voiceMenu } from "./board.js";

const SVGNS = "http://www.w3.org/2000/svg";
const ICONS = {
  board: "M3 3.5h4v4H3zM9 3.5h4v4H9zM3 9.5h4v3H3zM9 9.5h4v3H9z",
  graph: "M4 4.5a1.5 1.5 0 1 0 0-.01M12 4.5a1.5 1.5 0 1 0 0-.01M8 12a1.5 1.5 0 1 0 0-.01M5.2 5.3l2 5M10.8 5.3l-2 5M5.5 4h5",
  chats: "M2.5 3.5h11v7h-6l-3 2.5v-2.5h-2z",
  sky: "M8 1.8l1.5 4.7 4.7 1.5-4.7 1.5L8 14.2l-1.5-4.7L1.8 8l4.7-1.5z",
  settings: "M8 5.6a2.4 2.4 0 1 0 0 4.8 2.4 2.4 0 0 0 0-4.8zM8 1.8v1.6M8 12.6v1.6M1.8 8h1.6M12.6 8h1.6M3.6 3.6l1.1 1.1M11.3 11.3l1.1 1.1M3.6 12.4l1.1-1.1M11.3 4.7l1.1-1.1",
};
function glyph(name) {
  const s = document.createElementNS(SVGNS, "svg");
  s.setAttribute("viewBox", "0 0 16 16"); s.setAttribute("aria-hidden", "true");
  const p = document.createElementNS(SVGNS, "path");
  for (const [k, v] of Object.entries({ d: ICONS[name], fill: "none", stroke: "currentColor", "stroke-width": "1.5", "stroke-linecap": "round", "stroke-linejoin": "round" })) p.setAttribute(k, v);
  s.append(p);
  return s;
}

export function lookFromUrl() {
  const q = new URLSearchParams(location.search).get("look");
  return q === "nebula" || q === "current" ? q : null;
}
export function currentLook() { return lookFromUrl() || store.get("look", "current"); }

export function initNebula(ui) {
  const root = document.documentElement;
  const phoneQ = matchMedia("(max-width: 760px)");
  const shell = document.getElementById("shell");
  const host = document.body.insertBefore(h("div", { class: "nb-field", "aria-hidden": "false" }), document.body.firstChild);
  const labels = h("div", { class: "nb-labels", "aria-hidden": "true" });
  const band = h("div", { class: "nb-band", "aria-hidden": "true" });
  shell.before(band);
  const field = new Field(host, { band, labels, phone: () => phoneQ.matches });
  host.append(labels);

  // Pull into the sky: at the top of the board, keep scrolling up (or pull down on a phone) and the nebula comes
  // closer; past the threshold it dives into Sky. Transforms only (compositor), so the pull never relayouts.
  const PULL = 260;
  let pull = 0, pullIdle = null, touchY = null;
  const setPull = (v) => {
    pull = Math.max(0, Math.min(PULL, v));
    root.style.setProperty("--nb-pull", (pull / PULL).toFixed(3));
    root.classList.toggle("nb-pulling", pull > 0);
  };
  const scrolledInside = (t) => { for (let el = t; el && el !== document.body; el = el.parentElement) if (el.scrollTop > 0) return true; return false; };
  const atTop = (t) => root.dataset.look === "nebula" && ui.route() === "board" && window.scrollY <= 0 && !scrolledInside(t)
    && !(t && t.closest && t.closest("input, textarea, select, dialog, .pop"));
  const dive = () => {
    clearTimeout(pullIdle);
    root.classList.add("nb-dive");
    setTimeout(() => { location.hash = "#/sky"; root.classList.remove("nb-dive"); setPull(0); },
      matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 420);
  };
  addEventListener("wheel", (e) => {
    if (e.deltaY >= 0 || e.ctrlKey || !atTop(e.target)) { if (pull) setPull(0); return; }
    setPull(pull - e.deltaY * 0.6);
    clearTimeout(pullIdle);
    if (pull >= PULL) dive(); else pullIdle = setTimeout(() => setPull(0), 350);
  }, { passive: true });
  addEventListener("touchstart", (e) => { touchY = e.touches.length === 1 && atTop(e.target) ? e.touches[0].clientY : null; }, { passive: true });
  addEventListener("touchmove", (e) => { if (touchY != null) setPull((e.touches[0].clientY - touchY) * 1.3); }, { passive: true });
  addEventListener("touchend", () => { if (touchY == null) return; touchY = null; if (pull >= PULL * 0.6) dive(); else setPull(0); }, { passive: true });

  const capsule = h("nav", { class: "nb-capsule", "aria-label": "Views" },
    [["#/sky", "sky", "Sky"], ["#/", "board", "Board"], ["#/graph", "graph", "Graph"], ["#/sessions", "chats", "Chats"], ["#/settings", "settings", "Settings"]]
      .map(([href, id, label]) => h("a", { href, dataset: { view: id } }, glyph(id), h("span", {}, label))));
  document.body.append(capsule);

  // Actions ornament on the centre window's bottom edge. Dismiss acts on the selected question (j/k or a click).
  const orn = h("div", { class: "nb-orn", role: "toolbar", "aria-label": "Board actions" },
    h("button", { class: "btn ghost", title: "Dismiss the selected question without answering (logged)", onclick: () => {
      const el = ui.keyed && document.querySelector(`#s-${ui.cur} .dismiss`);
      if (el) el.click(); else toast("Select a question first: j/k, or click it");
    } }, "Dismiss"),
    h("button", { class: "btn ghost", onclick: () => voiceMenu(ui) }, "Voice"),
    h("button", { class: "btn ghost", onclick: () => ui.spawn({}) }, "New chat"));
  shell.append(orn);
  document.getElementById("main").addEventListener("click", (e) => {
    if (!on()) return;
    const li = e.target.closest(".strip");
    if (!li || !li.id.startsWith("s-")) return;
    const i = Number(li.id.slice(2));
    if (ui.cur !== i || !ui.keyed) { ui.cur = i; ui.keyed = true; ui.markCur(); }   // a class flip, not a full render
  });

  // ?fps=1: on-page frame-time readout (median and p95 over the last 30 s of running frames)
  const fpsOn = new URLSearchParams(location.search).get("fps") === "1";
  let fpsEl = null;
  if (fpsOn) {
    field.statsOn = true;
    fpsEl = document.body.appendChild(h("div", { class: "nb-fps", role: "status" }));
    setInterval(() => {
      const cut = 1800;                 // ~30 s at 60 Hz
      if (field.dts.length > cut) field.dts.splice(0, field.dts.length - cut);
      const st = field.stats();
      fpsEl.textContent = st.n ? `frame ms: median ${st.median.toFixed(1)}, p95 ${st.p95.toFixed(1)}, n ${st.n}${field.raf ? "" : " (paused)"}` : `frame ms: no running frames${field.raf ? "" : " (paused)"}`;
    }, 1000);
  }

  let last = null;
  function on() { return currentLook() === "nebula" && ui.route() === "board"; }
  function measure() {
    const r = shell.getBoundingClientRect();
    root.style.setProperty("--nb-top", Math.round(r.top + scrollY) + "px");
    const b = band.getBoundingClientRect();     // the night window's rect in field coordinates (light theme clip)
    root.style.setProperty("--nb-by", Math.round(b.top + scrollY) + "px");
    root.style.setProperty("--nb-bx", Math.round(b.left) + "px");
    root.style.setProperty("--nb-br", Math.round(document.documentElement.clientWidth - b.right) + "px");
    root.style.setProperty("--nb-bh", Math.round(b.height) + "px");
  }
  function apply() {
    const look = currentLook();
    if (look === "nebula") root.dataset.look = "nebula"; else delete root.dataset.look;
    root.dataset.view = ui.route();
    capsule.querySelectorAll("a").forEach((a) => a.setAttribute("aria-current", a.dataset.view === { sky: "sky", board: "board", work: "board", graph: "graph", sessions: "chats", chat: "chats", settings: "settings" }[ui.route()] ? "page" : "false"));
    const active = on();
    if (active) { measure(); if (last) field.update(last); }
    field.setActive(active);
  }
  function update(ov, graph, healthOk) {
    last = fieldModel(ov, graph, healthOk);
    if (!on()) return;
    measure();
    if (field.update(last)) window.__nebula.updates++;
  }
  addEventListener("resize", () => on() && measure());
  // specular rim: each window's edge is lit from where the pointer is
  for (const w of [document.getElementById("side"), document.querySelector("#shell > .col"), document.getElementById("cap")]) {
    w.addEventListener("pointermove", (e) => {
      if (!on()) return;
      const r = w.getBoundingClientRect();
      w.style.setProperty("--mx", (((e.clientX - r.left) / r.width) * 100).toFixed(1) + "%");
      w.style.setProperty("--my", (((e.clientY - r.top) / r.height) * 100).toFixed(1) + "%");
    }, { passive: true });
  }
  phoneQ.addEventListener("change", () => { if (field.model) { field.sig = ""; update(ui.overview(), ui.graphData(), last && last.ok); } });

  ui.look = currentLook;
  ui.setLook = (v) => {
    store.set("look", v);
    if (lookFromUrl()) { const u = new URL(location.href); u.searchParams.delete("look"); history.replaceState(null, "", u); }
    apply(); ui.rerender(); toast(`Look: ${v}`);
  };
  // For the acceptance scripts (ui/acceptance): the field's own model and what the last paint drew.
  window.__nebula = {
    field, updates: 0,
    get model() { return field.model; }, get drawn() { return field.drawn; },
    get frames() { return field.frames; }, get stillPaints() { return field.stillPaints; },
    get running() { return !!field.raf; }, stats: () => field.stats(),
  };
  return { apply, update };
}
