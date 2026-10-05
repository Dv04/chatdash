// Sky (#/sky): the fleet as an observatory. The same WebGL nebula as the board's field, in true 3D: seats float at
// different depths, the camera drifts and leans with the pointer, a click flies into a seat (spring), and a needs-you
// star opens its real question card (cards.js, same send path as the board). Real data only, same model as the board
// (field.js fieldModel), same pause rule: motion only while the window is focused and visible, 0 frames otherwise,
// one still frame on a data change.
import { h, ct, age, toast } from "./lib.js";
import { post } from "./api.js";
import { makeNebula } from "./gl-nebula.js";
import { fieldModel } from "./field.js";
import { cardBody, timeoutLine } from "./cards.js";
import { pace } from "./capacity.js";
import { splitNeeds } from "./board.js";

const TAU = Math.PI * 2;
let probe;
function rgb(css) {
  probe = probe || document.createElement("canvas").getContext("2d", { willReadFrequently: true });
  probe.clearRect(0, 0, 1, 1); probe.fillStyle = "#000"; probe.fillStyle = css; probe.fillRect(0, 0, 1, 1);
  const d = probe.getImageData(0, 0, 1, 1).data;
  return [d[0] / 255, d[1] / 255, d[2] / 255];
}

// a damped spring per scalar (bounce about 0.15): natural, never linear
function spring(x, v, target, dt, k = 38, c = 10.5) {
  const a = (target - x) * k - v * c;
  v += a * dt; x += v * dt;
  return [x, v];
}

// Write a label's position only when it moved a whole pixel: per-frame writes of unchanged values were most of
// Sky's style-recalc time.
function setXY(o, x, y) {
  const tx = Math.round(x), ty = Math.round(y);
  if (o.tx === tx && o.ty === ty) return;
  o.tx = tx; o.ty = ty;
  o.el.style.transform = `translate(${tx}px, ${ty}px)`;
}

export class Sky {
  constructor(el, ui) {
    this.el = el; this.ui = ui;
    this.canvas = el.appendChild(h("canvas", { class: "sky-gl", role: "img", "aria-label": "Sky" }));
    try { this.gl = makeNebula(this.canvas); this.gl.onswap = (nc) => { this.canvas = nc; this.layout(); }; } catch (e) { this.gl = null; this.glError = e.message; }
    this.layer = el.appendChild(h("div", { class: "sky-labels" }));
    this.hud = el.appendChild(h("div", { class: "sky-hud", role: "status", "aria-live": "polite" }));
    this.panel = el.appendChild(h("aside", { class: "sky-panel", hidden: true, "aria-label": "Seat" }));
    this.ask = el.appendChild(h("section", { class: "sky-ask", hidden: true, role: "dialog", "aria-label": "Question" }));
    this.model = null; this.sig = ""; this.seats = []; this.focus = null; this.hover = null; this.askItem = null;
    this.cam = { x: 0, y: 0.25, z: -1.2, yaw: 0, pitch: 0, vx: 0, vy: 0, vz: 0, vyaw: 0, vpitch: 0 };
    this.ptr = [0, 0];
    this.active = false; this.raf = 0; this.t = 0; this.last = 0; this.frames = 0; this.stillPaints = 0;
    this.reduced = matchMedia("(prefers-reduced-motion: reduce)");
    this.phoneQ = matchMedia("(max-width: 760px)");
    this.phoneQ.addEventListener("change", () => { if (this.model) { this.build(); if (this.active) { this.layout(); this.still(); } } });
    this.focused = document.hasFocus(); this.visible = document.visibilityState === "visible";
    addEventListener("focus", () => { this.focused = true; this.sync(); });
    addEventListener("blur", () => { this.focused = false; this.sync(); });
    document.addEventListener("visibilitychange", () => { this.visible = document.visibilityState === "visible"; this.sync(); });
    this.reduced.addEventListener("change", () => { this.sync(); this.still(); });
    addEventListener("resize", () => { if (this.active) { this.layout(); this.still(); } });
    el.addEventListener("pointermove", (e) => {
      this.ptr = [(e.clientX / innerWidth) * 2 - 1, (e.clientY / innerHeight) * 2 - 1];
      this.pick(e.clientX, e.clientY);
    }, { passive: true });
    this.canvas.addEventListener("click", (e) => this.click(e.clientX, e.clientY));
    addEventListener("keydown", (e) => this.key(e));
    this.loop = this.loop.bind(this);
    window.__sky = this;
  }

  // ------------------------------------------------------------------ run control (identical rule to the board)
  want() { return this.active && this.visible && this.focused && !this.reduced.matches && !!this.gl; }
  sync() {
    if (this.want()) { if (!this.raf) { this.last = 0; this.raf = requestAnimationFrame(this.loop); } }
    else if (this.raf) { cancelAnimationFrame(this.raf); this.raf = 0; }
  }
  setActive(on) {
    this.active = on;
    if (on) { this.layout(); this.still(); }
    this.sync();
  }
  loop(now) {
    this.raf = 0;
    if (!this.want()) return;
    this.frames++;
    const dt = this.last ? Math.min(100, now - this.last) : 16;
    this.last = now; this.t += dt;
    this.step(dt / 1000);
    this.paint();
    this.raf = requestAnimationFrame(this.loop);
  }
  still() { if (!this.active || !this.model || this.raf) return; this.stillPaints++; this.settle(); this.paint(); }

  // ------------------------------------------------------------------ data
  update(ov, graph, ok) {
    this.ov = ov;
    const m = fieldModel(ov, graph, ok);
    this.hudRender(ov, ok);
    if (m.sig === this.sig) { if (this.focus) this.panelRender(); return false; }
    this.sig = m.sig; this.model = m;
    this.build();
    if (this.askItem && !this.itemFor(this.askItem.session_id)) this.closeAsk();
    if (this.focus && !this.seats.find((s) => s.seat === this.focus)) this.unfocus();
    if (this.focus) this.panelRender();
    this.still();
    return true;
  }
  itemFor(sid) { return ((this.ov && this.ov.needs_you) || []).find((x) => x.session_id === sid); }
  seatRow(name) { return ((this.ov && this.ov.capacity && this.ov.capacity.seats) || []).find((s) => s.seat === name); }

  // world: seats on a gentle arc, alternating height, depth from 8.5 to 13
  build() {
    const N = this.model.seats.length || 1;
    this.seats = this.model.seats.map((s, i) => {
      const th = N > 1 ? -0.95 + (1.9 * i) / (N - 1) : 0;
      const old = this.seats.find((o) => o.seat === s.seat);
      const ga = i * 2.39996;                                        // golden angle: even, non-repeating heights and depths
      // a phone is portrait: the same seats run top to bottom, alternating sides, so the sky fills the tall screen
      const pos = this.phoneQ.matches
        ? { wx: (i % 2 ? 1 : -1) * (2.1 + Math.sin(ga) * 0.7), wy: -th * 9.2 + 1.1, wz: 10 + (Math.cos(ga) + 1) * 1.6 }
        : { wx: Math.sin(th) * 9.6, wy: Math.sin(ga) * 2.1 + 0.2, wz: 9.5 + (1 - Math.cos(th)) * 3.5 + (Math.cos(ga) + 1) * 2.6 };
      return { ...s, i, ...pos,
        wr: s.kind === "unknown" ? 0.7 : 0.42 + 0.9 * ((s.left || 0) / 100), spin: old ? old.spin : i * 1.37 };
    });
    this.layer.replaceChildren();
    if (!this.gl) this.layer.append(h("p", { class: "sky-nogl" }, `The nebula needs WebGL2 (${this.glError}). Seats and numbers still work; the board shows the same data.`));
    for (const s of this.seats) {
      s.el = this.layer.appendChild(h("button", { class: "sky-cl k-" + s.kind, "aria-label": `${s.seat}: ${s.sub}. Fly in.`, onclick: () => this.flyTo(s.seat) },
        h("b", {}, s.seat), h("span", { class: "fig" }, s.left == null ? "?" : `${Math.round(s.left)}%`, s.left != null && h("small", {}, "left")),
        h("em", {}, [s.kind === "unknown" ? (s.unk || "unknown") : s.kind === "out" ? (s.left < 1 ? "used up" : "nearly out") : "", s.readonly ? "read-only" : "",
          s.chats.length ? `${s.chats.length} running${s.needs ? `, ${s.needs} need${s.needs === 1 ? "s" : ""} you` : ""}` : ""].filter(Boolean).join(", "))));
      s.lights = s.chats.map((c) => ({ ...c, el: this.layer.appendChild(h("div", { class: "sky-lt" + (c.needs ? " need" : " quiet") }, c.label)) }));
    }
    for (const s of this.seats) { s.cw = s.el.offsetWidth; s.ch = s.el.offsetHeight; for (const l of s.lights) { l.w = 0; l.h = 0; } }
    this.canvas.setAttribute("aria-label", "Sky: " + this.model.seats.map((c) => `${c.seat} ${c.sub}, ${c.chats.length} running`).join("; "));
  }

  layout() {
    const W = innerWidth, H = innerHeight, dpr = Math.min(devicePixelRatio || 1, 2);
    this.W = W; this.H = H;
    if (this.gl) { this.gl.gasScale = this.phoneQ.matches ? 0.4 : 0.5; this.gl.resize(W, H, dpr); }
  }

  // ------------------------------------------------------------------ camera
  target() {
    const s = this.focus && this.seats.find((x) => x.seat === this.focus);
    if (!s) {
      const drift = this.reduced.matches ? 0 : Math.sin(this.t / 23000) * 0.07;
      return { x: 0, y: 0.25, z: -1.2, yaw: drift + this.ptr[0] * 0.05, pitch: -this.ptr[1] * 0.03 };
    }
    const d = s.wr * 3.4 + 1.2, side = this.phoneQ.matches ? 0 : 0.9;    // leave room for the panel on the right
    return { x: s.wx + side * s.wr * 0.9, y: s.wy + 0.1, z: s.wz - d, yaw: this.ptr[0] * 0.02, pitch: -this.ptr[1] * 0.015 };
  }
  step(dt) {
    const T = this.target(), c = this.cam;
    for (const k of ["x", "y", "z", "yaw", "pitch"]) { const v = "v" + k; [c[k], c[v]] = spring(c[k], c[v], T[k], dt); }
  }
  settle() { const T = this.target(); Object.assign(this.cam, T, { vx: 0, vy: 0, vz: 0, vyaw: 0, vpitch: 0 }); }
  project(x, y, z) {
    const c = this.cam;
    let dx = x - c.x, dy = y - c.y, dz = z - c.z;
    const cy = Math.cos(c.yaw), sy = Math.sin(c.yaw);
    [dx, dz] = [dx * cy - dz * sy, dx * sy + dz * cy];
    const cp = Math.cos(c.pitch), sp = Math.sin(c.pitch);
    [dy, dz] = [dy * cp - dz * sp, dy * sp + dz * cp];
    if (dz < 0.3) return null;
    const f = Math.min(this.W, this.H * 1.6) * 0.74;
    return { x: this.W / 2 + (f * dx) / dz, y: this.H / 2 - (f * dy) / dz, s: f / dz, z: dz };
  }

  // ------------------------------------------------------------------ paint
  paint() {
    if (!this.model) return;
    if (!this.W) this.layout();
    const cs = getComputedStyle(this.el), tok = (n) => cs.getPropertyValue(n).trim();   // Sky is night in both themes
    const key = tok("--nb-seat") + tok("--nb-blend");
    if (key !== this.colKey) {
      this.colKey = key;
      this.col = { hue: rgb(tok("--nb-seat")), hue2: rgb(tok("--nb-seat-2")), out: rgb(tok("--nb-out")), out2: rgb(tok("--nb-out-2")), unk: rgb(tok("--nb-unk")),
        light: rgb(tok("--nb-chat")), need: rgb(tok("--nb-need")), ink: rgb(tok("--nb-ink")), dark: tok("--nb-blend") === "lighter" };
    }
    const t = this.t, still = !this.want(), clusters = [];
    this.fixed = this.fixedRects();                  // read before any style write this frame: no forced layout
    for (const s of this.seats) {
      const p = this.project(s.wx, s.wy, s.wz);
      s.p = p;
      if (!p) { s.el.hidden = true; s.lights.forEach((l) => (l.el.hidden = true)); continue; }
      const R = s.wr * p.s;
      const fade = Math.max(0.35, Math.min(1, 1.5 - p.z / 18));
      const lights = s.lights.map((l, j) => {
        const a = s.spin + (j / Math.max(1, s.lights.length)) * TAU + t * 0.00006;
        const rr = s.wr * 1.55;
        const q = this.project(s.wx + Math.cos(a) * rr, s.wy + Math.sin(a) * rr * 0.28, s.wz + Math.sin(a) * rr);
        l.p = q;
        return q && { x: q.x, y: q.y, needs: l.needs, idle: l.state === "idle" && !l.needs };
      }).filter(Boolean);
      clusters.push({ x: p.x, y: p.y, R, left: (s.left || 0) / 100, kind: s.kind, seed: s.i * 3.7 + 1.3, fade, lights, z: p.z });
      // callout under the cluster; hidden for the seat you are inside (the panel carries it)
      s.el.hidden = this.focus === s.seat;
      s.el.style.opacity = Math.max(0.75, fade).toFixed(2);
      s.el.style.zIndex = String(100 - Math.round(p.z));         // nearer seats sit in front
    }
    clusters.sort((a, b) => a.z - b.z);            // near first: the renderer composites front to back
    const c = this.cam;
    if (this.gl) this.gl.render({ clusters, dark: this.col.dark, dust: this.phoneQ.matches ? 350 : 1300, ...this.col, still, ring: this.phoneQ.matches ? 0.7 : 1.2,
      zone: 0, star: [c.yaw * 900 + c.x * 40, -c.pitch * 900 + c.y * 40] }, t);
    this.placeLabels();
  }

  // Labels never pile up. Seat callouts: nearer seats keep their spot, a farther callout that would overlap is
  // nudged below it. Star names (needs-you first, then hover, then the seat you are inside) try right, left, above,
  // below their star and take the first spot that is on screen and clear of every label, the HUD, the capsule and the
  // open glass; with no clear spot a name waits for hover.
  fixedRects() {
    const R = (el) => { const r = el.getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; };
    const out = [R(this.hud)];
    const cap = document.querySelector(".nb-capsule"); if (cap && cap.offsetParent) out.push(R(cap));
    if (!this.panel.hidden) out.push(R(this.panel));
    if (!this.ask.hidden) out.push(R(this.ask));
    return out;
  }
  placeLabels() {
    const hit = (a, b) => a[0] < b[0] + b[2] + 3 && b[0] < a[0] + a[2] + 3 && a[1] < b[1] + b[3] + 2 && b[1] < a[1] + a[3] + 2;
    const taken = [...(this.fixed || [])];
    const seats = this.seats.filter((s) => s.p && !s.el.hidden).sort((a, b) => a.p.z - b.p.z);
    for (const s of seats) {
      let x = s.p.x - s.cw / 2, y = s.p.y + s.wr * s.p.s * 0.7 + 6;
      for (let k = 0; k < 4; k++) {
        const o = taken.find((t) => hit([x, y, s.cw, s.ch], t));
        if (!o) break;
        y = o[1] + o[3] + 4;
      }
      x = Math.max(6, Math.min(this.W - s.cw - 6, x));
      setXY(s, x, y);
      taken.push([x, y, s.cw, s.ch]);
    }
    const lights = [];
    for (const s of this.seats) for (const l of s.lights) {
      const want = l.p && (l.needs || this.hover === l.id || this.focus === s.seat);
      if (!want) { if (!l.el.hidden) l.el.hidden = true; continue; }
      lights.push({ s, l, pr: this.hover === l.id ? 0 : l.needs ? 1 : 2 });
    }
    lights.sort((a, b) => a.pr - b.pr);
    for (const { l, pr } of lights) {
      if (l.el.hidden) l.el.hidden = false;
      if (!l.w) { l.w = l.el.offsetWidth; l.h = l.el.offsetHeight; }
      const { x, y } = l.p, w = l.w, h2 = l.h;
      const spots = [[x + 14, y - h2 / 2], [x - 14 - w, y - h2 / 2], [x - w / 2, y - 14 - h2], [x - w / 2, y + 14]];
      const ok = spots.find(([sx, sy]) => sx >= 6 && sy >= 6 && sx + w <= this.W - 6 && sy + h2 <= this.H - 6 && !taken.some((t) => hit([sx, sy, w, h2], t)));
      const spot = ok || (pr === 0 ? spots[0] : null);
      if (!spot) { l.el.hidden = true; continue; }
      setXY(l, spot[0], spot[1]);
      taken.push([spot[0], spot[1], w, h2]);
    }
  }

  // ------------------------------------------------------------------ interaction
  pick(x, y) {
    let best = null, bd = 20 * 20;
    for (const s of this.seats) for (const l of s.lights || []) {
      if (!l.p) continue;
      const d = (l.p.x - x) ** 2 + (l.p.y - y) ** 2;
      if (d < bd) { bd = d; best = l.id; }
    }
    this.canvas.style.cursor = best || this.seatAt(x, y) ? "pointer" : "";
    if (best !== this.hover) { this.hover = best; if (!this.raf && this.active) this.paint(); }
  }
  seatAt(x, y) {
    return this.seats.find((s) => s.p && (s.p.x - x) ** 2 + (s.p.y - y) ** 2 < (s.wr * s.p.s) ** 2);
  }
  click(x, y) {
    if (this.hover) {
      const it = this.itemFor(this.hover);
      if (it) return this.openAsk(it);
      const s = this.seats.find((q) => q.lights.some((l) => l.id === this.hover));
      if (s) return this.flyTo(s.seat);
    }
    const s = this.seatAt(x, y);
    if (s) this.flyTo(s.seat); else if (this.focus) this.unfocus();
  }
  key(e) {
    if (!this.active || /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
    if (e.key === "Escape") { if (!this.ask.hidden) this.closeAsk(); else if (this.focus) this.unfocus(); return; }
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      const i = this.seats.findIndex((s) => s.seat === this.focus);
      const n = this.seats.length, j = i < 0 ? (e.key === "ArrowRight" ? 0 : n - 1) : (i + (e.key === "ArrowRight" ? 1 : n - 1)) % n;
      this.flyTo(this.seats[j].seat); e.preventDefault();
    }
  }
  flyTo(seat) {
    this.focus = seat;
    this.panelRender();
    this.panel.hidden = false;
    if (!this.raf) this.still();                     // frozen or reduced motion: jump, no flight
  }
  unfocus() {
    this.focus = null; this.panel.hidden = true; this.closeAsk();
    if (!this.raf) this.still();
  }

  // ------------------------------------------------------------------ glass: HUD, seat panel, question
  hudRender(ov, ok) {
    const n = ov ? splitNeeds(ov).now.length : null, c = (ov && ov.counts) || {};
    this.hud.replaceChildren(
      h("span", { class: "sky-health " + (ok ? "ok" : "unknown") }, h("i", {}), ok ? "OK" : "UNKNOWN"),
      h("span", { class: "sky-count" }, h("b", {}, n ?? "?"), " need you"),
      h("span", { class: "sky-count" }, h("b", {}, c.sessions_working ?? "?"), " working"),
      this.focus ? h("button", { class: "sky-back", onclick: () => this.unfocus() }, "Back out", h("kbd", {}, "Esc")) : h("span", { class: "sky-hint" }, "Click a seat to fly in"));
  }

  panelRender() {
    const s = this.seats.find((x) => x.seat === this.focus);
    if (!s) return;
    const row = this.seatRow(s.seat) || {}, now = Date.now() / 1000;
    const w = (x, lbl) => {
      const k = x || {};
      return h("div", { class: "sky-win" }, h("span", { class: "k" }, lbl), h("b", {}, k.pct == null ? "?" : `${Math.round(k.pct)}%`),
        h("span", {}, k.pct == null ? (k.since_reset ? "reset since the last reading" : "no reading") : k.resets_at ? `resets ${ct(k.resets_at, true)} (in ${age(k.resets_at - now)})` : ""));
    };
    const p = row.seven_day ? pace(row, now, (this.ui.pace && this.ui.pace.pct_per_day) || 14.3) : { state: "unknown" };
    const chats = s.chats.map((c) => {
      const it = c.needs && this.itemFor(c.id);
      return h("li", {}, h("span", { class: "st " + (c.needs ? "needs_you" : c.state) }),
        it ? h("button", { class: "nm ask-open", onclick: () => this.openAsk(it) }, h("span", { class: "lbl" }, c.label), h("span", { class: "go" }, "Answer"))
          : h("a", { class: "nm", href: "#/chat/" + encodeURIComponent(c.id) }, c.label),
        h("span", { class: "rt" }, c.needs ? "needs you" : c.state));
    });
    this.panel.replaceChildren(...[         // replaceChildren would print a null as text: filter first
      h("header", {}, h("h2", {}, s.seat), h("span", { class: "fig" }, s.left == null ? "?" : `${Math.round(s.left)}%`, h("small", {}, "week left"))),
      h("p", { class: "sky-state k-" + s.kind }, s.sub),
      w(row.seven_day, "7d"), w(row.five_hour, "5h"),
      row.resume_at && h("p", { class: "sky-resume" }, `Resumes ${ct(row.resume_at)}`),
      p.state !== "unknown" && !row.excluded && h("p", { class: "sky-pace" }, p.state === "use" ? `Use first: about ${Math.round(p.extra)}% spare at the even pace` :
        p.state === "slow" ? "Ahead of pace: slow down here" : p.state === "spent" ? "7-day limit used up" : "On pace"),
      row.meter_age_min != null && h("p", { class: "sky-meta" }, `Meter reading ${row.meter_age_min >= 120 ? Math.round(row.meter_age_min / 60) + " h" : row.meter_age_min + " min"} old`,
        row.excluded ? ". Read-only seat: nothing here acts on it." : ""),
      h("h3", {}, "Chats", h("span", {}, String(s.chats.length))),
      s.chats.length ? h("ul", { class: "sky-chats" }, chats) : h("p", { class: "sky-meta" }, "Nothing running on this seat.")].filter(Boolean));
  }

  openAsk(x) {
    this.askItem = x;
    const rerender = () => this.openAsk(this.itemFor(x.session_id) || x);
    this.ask.replaceChildren(
      h("header", {},
        h("div", {}, h("h2", {}, x.title || "untitled"),
          h("div", { class: "tags" }, h("span", {}, x.kind), h("span", {}, x.seat), x.work_item && h("span", {}, x.work_item),
            x.risk && h("span", { class: "risk-" + x.risk }, x.risk + " risk"), h("span", {}, "waiting " + age(x.seconds)))),
        h("button", { class: "btn ghost", onclick: () => this.closeAsk(), "aria-label": "Close" }, "Close")),
      cardBody(x, this.ui, rerender),
      h("div", { class: "timeout" }, timeoutLine(x)),
      h("footer", {},
        h("a", { class: "btn ghost", href: "#/chat/" + encodeURIComponent(x.session_id) }, "Full chat history"),
        h("button", { class: "btn ghost", onclick: async () => {
          try { await post("needs/dismiss", { id: x.id, since: x.since, title: x.title }); toast("Dismissed"); this.closeAsk(); this.ui.refresh(); }
          catch (e) { toast(`Not dismissed: ${e.message}`); } } }, "Dismiss")));
    this.ask.hidden = false;
    const first = this.ask.querySelector(".opt, textarea, input");
    if (first) first.focus({ preventScroll: true });
  }
  closeAsk() { this.askItem = null; this.ask.hidden = true; }
}
