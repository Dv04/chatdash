// Nebula field engine (canvas 2D, no libraries). Real data only, never decoration:
//   one cluster per seat, size and density = share of the 7-day window left (100 - seven_day.pct);
//   a seat at >= 95% 7-day is drawn in the "nearly out" hue and labelled; colour alone never carries a state;
//   a seat with no usable 7-day reading (no reading, or reset since the reading) is a fixed-size hatched
//   cluster labelled "unknown", never sized from a guess. A read-only account is drawn from its own reading
//   (its 7d figure is real) and tagged read-only; a reading over an hour old says its age;
//   one light per running chat orbits its seat; a chat with an open NEEDS YOU item pulses white.
// Labels are DOM (crisp text, token colours, contrast-checked); the canvas draws particles only.
// Motion runs only while wanted (see Field.want); otherwise 0 frames, and a data change paints ONE still frame.
// Stage 2 views reuse the engine with their own camera (Field.camera: the world rect shown in the band).

import { GLNebula } from "./gl-nebula.js";
import { splitNeeds } from "./board.js";

const PHONE_MQ = matchMedia("(max-width: 760px)");
export const FRAME_MS = () => (PHONE_MQ.matches ? 1000 / 15 : 1000 / 24);

export const OUT_PCT = 95;

// ------------------------------------------------------------------ model (pure: feeds in, field state out)
export function fieldModel(ov, graph, healthOk, now = Date.now() / 1000) {
  const seats = (ov && ov.capacity && ov.capacity.seats) || [];
  const { now: needs, parked } = splitNeeds(ov);   // parked chats (seat at a limit) are not lit as needs-you
  const parkedSid = new Set(parked.map((x) => x.session_id));
  const needBySid = new Map();
  for (const x of needs) if (x.session_id) needBySid.set(x.session_id, (needBySid.get(x.session_id) || 0) + 1);
  const chats = new Map();
  const push = (seat, c) => { if (!chats.has(seat)) chats.set(seat, []); chats.get(seat).push(c); };
  const seen = new Set();
  for (const n of graph ? graph.nodes : []) {
    if (n.type !== "session") continue;
    const sid = n.session_id || n.id.replace(/^session:[^:]+:/, "");
    const need = needBySid.has(sid);
    if (n.state === "stopped" && !need) continue;           // running = a live process, or a question still open
    seen.add(sid);
    push(n.seat, { id: sid, label: n.label || sid.slice(0, 8), state: need ? "needs_you" : parkedSid.has(sid) ? "idle" : n.state === "needs_you" ? "working" : n.state, needs: need });
  }
  for (const x of needs) {                                   // an open question whose chat the graph does not list
    if (!x.session_id || seen.has(x.session_id)) continue;
    seen.add(x.session_id);
    push(x.seat, { id: x.session_id, label: x.title || x.session_id.slice(0, 8), state: "needs_you", needs: true });
  }
  const names = seats.map((s) => s.seat);
  for (const k of chats.keys()) if (k && !names.includes(k)) names.push(k);   // chats on a seat with no capacity row
  const out = names.map((name) => {
    const s = seats.find((x) => x.seat === name);
    const w = (s && s.seven_day) || {};
    const why = !s ? "no capacity row" : w.pct == null ? (w.since_reset ? "reset since the reading" : "no reading")
      : w.resets_at && w.resets_at < now ? "reset since the reading" : null;
    const left = why ? null : Math.max(0, Math.min(100, 100 - w.pct));
    const kind = why ? "unknown" : w.pct >= OUT_PCT ? "out" : "ok";
    const used = kind === "out" && w.pct >= 100;
    const age = s && s.meter_age_min != null && s.meter_age_min > 60 ? s.meter_age_min : null;
    const sub = (kind === "unknown" ? "unknown" : kind === "out" ? `${used ? "used up" : "nearly out"}, ${Math.round(left)}% left` : `${Math.round(left)}% left`)
      + (s && s.excluded ? ", read-only" : "") + (age ? `, reading ${age >= 120 ? Math.round(age / 60) + " h" : age + " min"} old` : "");
    const list = (chats.get(name) || []).sort((a, b) => b.needs - a.needs || a.id.localeCompare(b.id));
    return { seat: name, kind, left, why, sub, readonly: !!(s && s.excluded), stale: age, hatched: kind === "unknown", chats: list,
      needs: list.filter((c) => c.needs).length };
  });
  const sig = JSON.stringify([!!ov, !!healthOk, out.map((c) => [c.seat, c.kind, c.left == null ? null : Math.round(c.left), c.chats.map((x) => x.id + x.state)])]);
  return { seats: out, ok: !!healthOk, loaded: !!ov, needs: out.reduce((t, c) => t + c.needs, 0),
    running: out.reduce((t, c) => t + c.chats.length, 0), sig };
}

// ------------------------------------------------------------------ engine
const TAU = Math.PI * 2;
const TOKENS = ["--nb-seat", "--nb-out", "--nb-unk", "--nb-chat", "--nb-need", "--nb-star", "--nb-seat-2", "--nb-out-2", "--nb-ink"];
let probe;
function rgb(css) {                     // any CSS colour (oklch included) to 0..1 sRGB, via a 1 px canvas
  probe = probe || document.createElement("canvas").getContext("2d", { willReadFrequently: true });
  probe.clearRect(0, 0, 1, 1); probe.fillStyle = "#000"; probe.fillStyle = css; probe.fillRect(0, 0, 1, 1);
  const d = probe.getImageData(0, 0, 1, 1).data;
  return [d[0] / 255, d[1] / 255, d[2] / 255];
}

function rng(seed) {                    // deterministic per seat: a refresh never reshuffles a cluster
  let t = 0;
  for (const ch of seed) t = (t * 31 + ch.charCodeAt(0)) >>> 0;
  return () => { t += 0x6d2b79f5; let r = Math.imul(t ^ (t >>> 15), 1 | t); r ^= r + Math.imul(r ^ (r >>> 7), 61 | r); return ((r ^ (r >>> 14)) >>> 0) / 4294967296; };
}

function sprite(color, size = 64) {     // soft round glow, drawn once per colour and reused with drawImage
  const c = document.createElement("canvas");
  c.width = c.height = size;
  const g = c.getContext("2d");
  const gr = g.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
  gr.addColorStop(0, color); gr.addColorStop(0.25, color); gr.addColorStop(1, "transparent");
  g.globalAlpha = 1; g.fillStyle = gr; g.fillRect(0, 0, size, size);
  return c;
}

export class Field {
  constructor(host, { band, labels, phone }) {
    this.host = host; this.band = band; this.layer = labels; this.phone = phone;
    this.canvas = host.appendChild(document.createElement("canvas"));
    this.canvas.className = "nb-canvas";
    this.canvas.setAttribute("role", "img");
    this.ctx = this.canvas.getContext("2d");
    // WebGL2 volumetric renderer when available (?gl0 forces the 2D painter); the 2D painter is the fallback
    this.gl = null;
    if (!new URLSearchParams(location.search).has("gl0")) {
      try {
        const c = document.createElement("canvas");
        c.className = "nb-gl"; c.setAttribute("role", "img");
        this.gl = new GLNebula(c);
        this.glCanvas = host.insertBefore(c, this.canvas);
        this.canvas.removeAttribute("role"); this.canvas.style.display = "none";
      } catch { this.gl = null; }
    }
    this.mode = this.gl ? "gl" : "2d";
    this.hover = null;
    addEventListener("pointermove", (e) => this.pointer(e), { passive: true });
    this.camera = { x: 0, y: 0, w: 1, h: 1 };       // world rect shown in the band (stage 2 moves it)
    this.model = null; this.sig = ""; this.clusters = []; this.stars = null;
    this.active = false; this.raf = 0; this.t = 0; this.last = 0;
    this.frames = 0; this.stillPaints = 0; this.drawn = [];
    this.dts = []; this.statsOn = false;
    this.reduced = matchMedia("(prefers-reduced-motion: reduce)");
    this.focused = document.hasFocus(); this.visible = document.visibilityState === "visible";
    addEventListener("focus", () => { this.focused = true; this.sync(); });
    addEventListener("blur", () => { this.focused = false; this.sync(); });
    document.addEventListener("visibilitychange", () => { this.visible = document.visibilityState === "visible"; this.sync(); });
    // scrolled out of view (the long phone board) = no frames, so scrolling never competes with the GPU
    this.inView = true;
    if ("IntersectionObserver" in window) new IntersectionObserver((es) => { this.inView = es[es.length - 1].isIntersecting; this.sync(); }).observe(band);
    this.reduced.addEventListener("change", () => { this.sync(); this.paintStill(); });
    addEventListener("resize", () => { this.layout(); this.paintStill(); });
    this.loop = this.loop.bind(this);
  }

  // Full motion only while the look is on, the tab is visible and the window focused, and motion is allowed.
  want() { return this.active && this.visible && this.focused && this.inView && !this.reduced.matches; }
  sync() {
    if (this.want()) { if (!this.raf) { this.last = 0; this.raf = requestAnimationFrame(this.loop); } }
    else if (this.raf) { cancelAnimationFrame(this.raf); clearTimeout(this.nap); this.raf = 0; }
  }
  setActive(on) {
    this.active = on;
    this.canvas.hidden = !on;
    if (this.glCanvas) this.glCanvas.hidden = !on;
    if (on) { this.layout(); this.paintStill(); }
    this.sync();
  }

  colors() {
    const cs = getComputedStyle(this.host);   // the field's own tokens: a night window inside the light theme
    const key = TOKENS.map((t) => cs.getPropertyValue(t).trim()).join("|");
    if (key === this.colorKey) return;
    this.colorKey = key;
    const [seat, out, unk, chat, need, star, seat2, out2, ink] = key.split("|");
    this.col = { seat, out, unk, chat, need, star, dark: cs.getPropertyValue("--nb-blend").trim() === "lighter" };
    this.lin = { hue: rgb(seat), hue2: rgb(seat2 || seat), out: rgb(out), out2: rgb(out2 || out), unk: rgb(unk), light: rgb(chat), need: rgb(need), ink: rgb(ink || "#1a1f40") };
    this.spr = { seat: sprite(seat), out: sprite(out), chat: sprite(chat), need: sprite(need) };
  }

  layout() {
    const dpr = Math.min(devicePixelRatio || 1, this.phone() ? 2 : 1.5);
    const W = document.documentElement.clientWidth, H = innerHeight;
    if (this.canvas.width !== Math.round(W * dpr) || this.canvas.height !== Math.round(H * dpr)) {
      this.canvas.width = Math.round(W * dpr); this.canvas.height = Math.round(H * dpr);
      this.canvas.style.width = W + "px"; this.canvas.style.height = H + "px";
    }
    this.dpr = dpr; this.W = W; this.H = H;
    const r = this.band.getBoundingClientRect();
    this.rect = { x: r.left + scrollX, y: r.top + scrollY, w: r.width, h: r.height };
    if (!this.stars || this.stars.phone !== this.phone()) this.buildStars();
    if (this.gl) {                       // the GL canvas covers the band plus a 40 px bleed above it, nothing below
      const g = { x: 0, y: this.rect.y - 40, w: W, h: this.rect.h + 40 };   // 40 px bleed: nebula.css --nb-win-clip-gl must match
      this.glRect = g;
      Object.assign(this.glCanvas.style, { left: g.x + "px", top: g.y + "px", width: g.w + "px", height: g.h + "px" });
      this.gl.gasScale = this.phone() ? 0.5 : 0.75;
      this.gl.resize(g.w, g.h, Math.min(devicePixelRatio || 1, 2));
    }
    this.place();
  }

  buildStars() {
    const n = this.phone() ? 70 : 210, r = rng("stars");
    const s = { phone: this.phone(), x: new Float32Array(n), y: new Float32Array(n), v: new Float32Array(n), z: new Float32Array(n) };
    for (let i = 0; i < n; i++) { s.x[i] = r(); s.y[i] = r(); s.v[i] = 0.000004 + r() * 0.00001; s.z[i] = 0.6 + r() * 1.2; }
    this.stars = s;
  }

  // World to screen through the camera, inside the band rect.
  toScreen(u, v) {
    const c = this.camera, b = this.rect;
    return [b.x + ((u - c.x) / c.w) * b.w, b.y + ((v - c.y) / c.h) * b.h];
  }

  update(model) {
    this.colors();
    if (model.sig === this.sig) return false;
    this.sig = model.sig; this.model = model;
    this.build();
    this.layout();
    this.paintStill();
    return true;
  }

  // Particles per cluster. Count and radius scale with the share of the week left; unknown has none.
  build() {
    const k = this.phone() ? 1 / 3 : 1;
    const prev = new Map(this.clusters.map((c) => [c.seat, c]));
    this.clusters = this.model.seats.map((s, i) => {
      const c = { ...s, i, n: 0 };
      if (s.kind !== "unknown") {
        const f = s.left / 100, r = rng(s.seat);
        const n = Math.round((36 + 520 * f) * k);
        c.n = n; c.a = new Float32Array(n); c.d = new Float32Array(n); c.sp = new Float32Array(n); c.sz = new Float32Array(n); c.al = new Uint8Array(n);
        for (let j = 0; j < n; j++) {
          c.a[j] = r() * TAU; c.d[j] = Math.pow(r(), 1.6); c.sp[j] = (0.00005 + r() * 0.00016) * (r() < 0.5 ? -1 : 1);
          c.sz[j] = 0.7 + r() * 1.5; c.al[j] = Math.floor(r() * 4);
        }
      }
      const old = prev.get(s.seat);
      c.spin = old ? old.spin : rng(s.seat + "o")() * TAU;     // ring phase survives a refresh
      return c;
    });
    this.renderLabels();
  }

  // Cluster centres in world units. Desk: one cell per seat across the band, alternating heights, label on
  // the cluster. Phone: two staggered rows, label under the cluster (seven labels do not fit side by side).
  place() {
    const N = this.clusters.length || 1, b = this.rect, phone = this.phone();
    const cols = phone && N > 4 ? Math.ceil(N / 2) : N;
    const cellW = b.w / cols, cellH = phone && N > 4 ? b.h / 2 : b.h;
    for (const c of this.clusters) {
      const row = c.i < cols ? 0 : 1;
      const u = row ? (c.i - cols + 1) / cols : (c.i + 0.5) / cols;
      const v = phone ? (N > 4 ? (row ? 0.68 : 0.27) : 0.36) : N > 3 ? (c.i % 2 ? 0.44 : 0.3) : 0.36;
      const [x, y] = this.toScreen(u, v);
      c.x = x; c.y = y;
      const rmax = Math.max(12, Math.min(cellW * 0.3, cellH * (phone ? 0.28 : 0.27)));
      c.R = c.kind === "unknown" ? Math.min(30, rmax * 0.6) : rmax * (0.3 + 0.7 * (c.left / 100));
      c.ring = Math.max(c.R, phone ? 12 : 18) + (phone ? 8 : 14);
      c.ly = phone ? y + rmax * 0.62 + 4 : b.y + b.h - (c.h || 0) - 4;     // label top: under the cluster (phone), band foot (desk)
      if (c.el) {
        c.el.style.transform = `translate(${x}px, ${c.ly}px) translate(-50%, 0)`;
        c.el.style.setProperty("--lead", phone ? "0px" : Math.max(0, c.ly - (y + c.R * 0.7 + 8)).toFixed(0) + "px");
      }
    }
    if (this.noteEl) this.noteEl.style.transform = `translate(${b.x + 8}px, ${b.y + 8}px)`;
  }

  renderLabels() {
    const L = this.layer;
    L.replaceChildren();
    for (const c of this.clusters) {
      c.el = L.appendChild(document.createElement("div"));
      c.el.className = "nb-cl k-" + c.kind;
      const b = c.el.appendChild(document.createElement("b")); b.textContent = c.seat;
      const s = c.el.appendChild(document.createElement("span")); s.textContent = c.sub;
      s.className = "sub";
      const v = c.el.appendChild(document.createElement("span"));        // desk callout: big figure + state words
      v.className = "fig"; v.textContent = c.left == null ? "?" : Math.round(c.left) + "%";
      if (c.left != null) { const sm = v.appendChild(document.createElement("small")); sm.textContent = "left"; }
      const st = c.el.appendChild(document.createElement("em"));
      st.textContent = [c.kind === "unknown" ? "unknown" : c.kind === "out" ? (c.left < 1 ? "used up" : "nearly out") : "", c.readonly ? "read-only" : "",
        c.stale ? `reading ${c.stale >= 120 ? Math.round(c.stale / 60) + " h" : c.stale + " min"} old` : ""].filter(Boolean).join(", ");
      const k = c.el.appendChild(document.createElement("i"));      // phone: lights are unlabelled there, so the count is
      k.textContent = `${c.chats.length} running${c.needs ? `, ${c.needs} need${c.needs === 1 ? "s" : ""} you` : ""}`;
      c.lights = c.chats.map((ch) => {
        const el = L.appendChild(document.createElement("div"));
        el.className = "nb-lt" + (ch.needs ? " need" : " quiet") + (ch.state === "idle" ? " idle" : "");
        el.textContent = (ch.needs ? "needs you: " : "") + ch.label;
        return { ...ch, el };
      });
    }
    const m = this.model;
    const note = !m.loaded ? "No data yet: UNKNOWN" : !m.ok ? "Data not fresh: UNKNOWN (exact state in the rail)" : "";
    this.noteEl = null;
    if (note) { const n = this.noteEl = L.appendChild(document.createElement("div")); n.className = "nb-note"; n.textContent = note; }
    L.classList.toggle("stale", !m.ok);
    for (const c of this.clusters) {          // label sizes, read once per data change (never per frame)
      c.w = c.el.offsetWidth; c.h = c.el.offsetHeight;
      for (const l of c.lights) { l.w = l.el.offsetWidth; l.h = l.el.offsetHeight; l.hid = false; }
    }
    if (this.rect) this.place();
    (this.glCanvas || this.canvas).setAttribute("aria-label", "Fleet field: " + (m.seats.map((c) => `${c.seat} ${c.sub}, ${c.chats.length} running${c.needs ? `, ${c.needs} need you` : ""}`).join("; ") || "no seats") + ".");
  }

  loop(t) {
    this.raf = 0;
    if (!this.want()) return;
    // Frame cap (speed first). The gas drifts slowly, so 24 fps on a desk and 15 on a phone look the
    // same as 60 while costing a third to a quarter of the main thread and GPU (measured 740 ms busy per 10 s at 60).
    if (this.last && t - this.last < FRAME_MS() - 2) {             // sleep until the next capped frame instead of
      const wait = FRAME_MS() - (t - this.last);                     // waking the compositor 60 times a second
      this.raf = -1; this.nap = setTimeout(() => { this.raf = requestAnimationFrame(this.loop); }, wait); return;
    }
    this.frames++;
    if (this.last) {
      const dt = t - this.last;
      this.t += Math.min(dt, 100);
      if (this.statsOn) { this.dts.push(dt); if (this.dts.length > 4000) this.dts.splice(0, 1000); }
    }
    this.last = t;
    this.paint();
    this.raf = requestAnimationFrame(this.loop);
  }

  // A still frame: used while frozen (blur, hidden tab, reduced motion) when data, theme or size change.
  paintStill() {
    if (!this.active || !this.model || this.raf) return;
    this.stillPaints++;
    this.paint();
  }

  paint() {
    this.colors();
    if (this.gl) return this.paintGL();
    const g = this.ctx, dpr = this.dpr, col = this.col, t = this.t, still = !this.want();
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, this.W, this.H);
    g.globalCompositeOperation = col.dark ? "lighter" : "source-over";
    const fade = this.model.ok ? 1 : 0.4;            // stale data never looks as confident as fresh data
    // Everything is drawn inside the band rows only: behind the glass windows there is nothing but the CSS void
    // gradient, which is what contrast.py checks window text against (verified by pixel sampling in acceptance).
    const B = this.rect;
    g.save();
    g.beginPath(); g.rect(0, B.y, this.W, B.h); g.clip();
    // drifting background stars (depth, not data)
    const s = this.stars;
    g.fillStyle = col.star; g.globalAlpha = 0.55 * fade;
    g.beginPath();
    for (let i = 0; i < s.x.length; i++) {
      const x = ((s.x[i] + t * s.v[i]) % 1) * this.W, y = B.y + s.y[i] * B.h;
      g.rect(x, y, s.z[i], s.z[i]);
    }
    g.fill();
    const drawn = [];
    for (const c of this.clusters) {
      if (c.kind === "unknown") { this.hatch(c, fade); drawn.push({ seat: c.seat, kind: c.kind, hatched: true, particles: 0, color: col.unk, label: c.sub }); continue; }
      const hue = c.kind === "out" ? col.out : col.seat;
      g.globalAlpha = 0.5 * fade;
      const gs = c.R * 2.6;
      g.drawImage(this.spr[c.kind === "out" ? "out" : "seat"], c.x - gs / 2, c.y - gs * 0.33, gs, gs * 0.66);
      g.fillStyle = hue;
      for (let b = 0; b < 4; b++) {
        g.globalAlpha = (0.28 + b * 0.2) * fade;
        g.beginPath();
        for (let j = 0; j < c.n; j++) {
          if (c.al[j] !== b) continue;
          const a = c.a[j] + c.sp[j] * t * 1.6, d = c.d[j] * c.R, z = c.sz[j];
          g.rect(c.x + Math.cos(a) * d - z / 2, c.y + Math.sin(a) * d * 0.62 - z / 2, z, z);
        }
        g.fill();
      }
      drawn.push({ seat: c.seat, kind: c.kind, hatched: false, particles: c.n, color: hue, label: c.sub, radius: Math.round(c.R) });
    }
    // chat lights: evenly spaced on one or two rings so their labels rarely meet
    const pts = [];
    for (const c of this.clusters) {
      const n = c.lights.length;
      c.lights.forEach((l, i) => {
        const ring = n > 6 && i % 2 ? 1 : 0, perRing = n > 6 ? Math.ceil(n / 2) : n;
        const k = n > 6 ? Math.floor(i / 2) : i;
        const a = c.spin + (k / Math.max(1, perRing)) * TAU + ring * 0.4 + t * 0.00007 * (ring ? -1 : 1);
        const rx = c.ring + ring * 20, ry = rx * 0.55;
        const ca = Math.cos(a), sa = Math.sin(a);
        const x = c.x + ca * rx, y = c.y + sa * ry;
        let size = l.needs ? 26 : l.state === "idle" ? 12 : 18;
        if (l.needs) size += still ? 6 : 6 * Math.sin(t / 260);
        g.globalAlpha = (l.state === "idle" && !l.needs ? 0.55 : 1) * fade;
        g.drawImage(l.needs ? this.spr.need : this.spr.chat, x - size / 2, y - size / 2, size, size);
        pts.push({ l, x: x + ca * 9, y: y + sa * 9, ca, sa, pr: l.needs ? 0 : l.state === "idle" ? 2 : 1 });
      });
    }
    // Labels: needs-you first, then working, then idle. One that would cover another label fades out until
    // its light moves clear (every light keeps its label; only collisions wait).
    const taken = this.clusters.map((c) => [c.x - c.w / 2, c.ly, c.w, c.h]);
    pts.sort((p, q) => p.pr - q.pr);
    for (const p of pts) {
      const { l } = p;
      const r = [p.x - l.w * (0.5 - 0.5 * p.ca), p.y - l.h * (0.5 - 0.5 * p.sa), l.w, l.h];
      const hit = l.w > 0 && (r[0] < 2 || r[0] + r[2] > this.W - 2 || r[1] < B.y || r[1] + r[3] > B.y + B.h || taken.some((o) => r[0] < o[0] + o[2] + 2 && o[0] < r[0] + r[2] + 2 && r[1] < o[1] + o[3] + 1 && o[1] < r[1] + r[3] + 1));
      const off = hit || (!l.needs && this.hover !== l.id);
      if (!off) taken.push(r);
      if (off !== l.hid) { l.hid = off; l.el.classList.toggle("hid", off); }
      p.l.sx = p.x; p.l.sy = p.y;
      l.el.style.transform = `translate(${p.x.toFixed(1)}px, ${p.y.toFixed(1)}px) translate(${(-50 + 50 * p.ca).toFixed(0)}%, ${(-50 + 50 * p.sa).toFixed(0)}%)`;
    }
    g.restore();
    g.globalAlpha = 1; g.globalCompositeOperation = "source-over";
    for (const c of this.clusters) c.spinNow = c.spin + t * 0.00007;
    this.drawn = drawn;
  }

  // GL path: the same geometry as the 2D painter, handed to the volumetric renderer.
  paintGL() {
    const t = this.t, still = !this.want(), B = this.rect, G = this.glRect;
    const pts = [], drawn = [];
    const clusters = this.clusters.map((c) => {
      const n = c.lights.length;
      const lights = c.lights.map((l, i) => {
        const ring = n > 6 && i % 2 ? 1 : 0, perRing = n > 6 ? Math.ceil(n / 2) : n, k = n > 6 ? Math.floor(i / 2) : i;
        const a = c.spin + (k / Math.max(1, perRing)) * TAU + ring * 0.4 + t * 0.00007 * (ring ? -1 : 1);
        const rx = c.ring + ring * 20, ry = rx * 0.55, ca = Math.cos(a), sa = Math.sin(a);
        const x = c.x + ca * rx, y = c.y + sa * ry;
        pts.push({ l, x: x + ca * 12, y: y + sa * 12, ca, sa, pr: l.needs ? 0 : l.state === "idle" ? 2 : 1 });
        l.sx = x; l.sy = y;
        return { x: x - G.x, y: y - G.y, needs: l.needs, idle: l.state === "idle" && !l.needs };
      });
      drawn.push({ seat: c.seat, kind: c.kind, hatched: c.kind === "unknown", particles: c.kind === "unknown" ? 0 : 1 + Math.round(900 * (0.15 + (c.left || 0) / 100) / 7),
        color: c.kind === "out" ? this.col.out : c.kind === "unknown" ? this.col.unk : this.col.seat, label: c.sub, radius: Math.round(c.R), renderer: "gl" });
      return { x: c.x - G.x, y: c.y - G.y, R: c.R * 1.15, left: (c.left || 0) / 100, kind: c.kind, seed: c.i * 3.7 + 1.3, lights };
    });
    const L = this.lin;
    this.gl.render({ clusters, dark: this.col.dark, dust: this.phone() ? 300 : 900, hue: L.hue, hue2: L.hue2, out: L.out, out2: L.out2,
      unk: L.unk, light: L.light, need: L.need, ink: L.ink, still, ring: this.phone() ? 0.55 : 1, zone: this.zone() }, t);
    this.labels(pts, B);
    this.drawn = drawn;
  }

  // Height of the callout floor at the band's foot (desk), in CSS px; 0 on the phone (labels sit under clusters).
  zone() {
    if (this.phone() || !this.clusters.length) return 0;
    return Math.ceil(Math.max(...this.clusters.map((c) => c.h || 0))) + 8;
  }

  // Needs-you lights are always named; any other light shows its name while the pointer is on it.
  labels(pts, B) {
    const taken = this.clusters.map((c) => [c.x - c.w / 2, c.ly, c.w, c.h]);
    pts.sort((p, q) => p.pr - q.pr);
    for (const p of pts) {
      const { l } = p;
      const r = [p.x - l.w * (0.5 - 0.5 * p.ca), p.y - l.h * (0.5 - 0.5 * p.sa), l.w, l.h];
      const hit = l.w > 0 && (r[0] < 2 || r[0] + r[2] > this.W - 2 || r[1] < B.y - 30 || r[1] + r[3] > B.y + B.h || taken.some((o) => r[0] < o[0] + o[2] + 2 && o[0] < r[0] + r[2] + 2 && r[1] < o[1] + o[3] + 1 && o[1] < r[1] + r[3] + 1));
      const off = (hit && this.hover !== l.id) || (!l.needs && this.hover !== l.id);
      if (!off) taken.push(r);
      if (off !== l.hid) { l.hid = off; l.el.classList.toggle("hid", off); }
      // Only a visible label is moved, and only when it moved a pixel: writing every hidden label each frame was
      // most of the field's style-recalc time (most lights are unlabelled until hovered).
      if (off) continue;
      const tx = Math.round(p.x), ty = Math.round(p.y), ax = Math.round(-50 + 50 * p.ca), ay = Math.round(-50 + 50 * p.sa);
      if (l.tx === tx && l.ty === ty && l.ax === ax && l.ay === ay) continue;
      l.tx = tx; l.ty = ty; l.ax = ax; l.ay = ay;
      l.el.style.transform = `translate(${tx}px, ${ty}px) translate(${ax}%, ${ay}%)`;
    }
  }

  pointer(e) {
    if (!this.active || !this.rect) return;
    const x = e.pageX, y = e.pageY;
    let best = null, bd = 18 * 18;
    for (const c of this.clusters) for (const l of c.lights || []) {
      if (l.sx == null) continue;
      const d = (l.sx - x) ** 2 + (l.sy - y) ** 2;
      if (d < bd) { bd = d; best = l.id; }
    }
    if (best !== this.hover) { this.hover = best; if (!this.raf) this.paintStillHover(); }
  }
  paintStillHover() { if (this.active && this.model) this.paint(); }    // a hover is not a data change: not counted

  // Unknown: fixed size, dashed rim, diagonal hatch. The size never encodes a guess.
  hatch(c, fade) {
    const g = this.ctx, R = c.R;
    g.save();
    g.globalCompositeOperation = "source-over";
    g.globalAlpha = 0.75 * fade;
    g.strokeStyle = this.col.unk; g.lineWidth = 1.2;
    g.setLineDash([4, 4]);
    g.beginPath(); g.ellipse(c.x, c.y, R, R * 0.62, 0, 0, TAU); g.stroke();
    g.clip();
    g.setLineDash([]); g.globalAlpha = 0.35 * fade;
    g.beginPath();
    for (let d = -R * 2; d < R * 2; d += 6) { g.moveTo(c.x + d - R, c.y + R); g.lineTo(c.x + d + R, c.y - R); }
    g.stroke();
    g.restore();
  }

  stats() {
    const a = [...this.dts].sort((x, y) => x - y);
    const q = (p) => (a.length ? a[Math.min(a.length - 1, Math.floor(p * a.length))] : null);
    return { n: a.length, median: q(0.5), p95: q(0.95), max: a.length ? a[a.length - 1] : null };
  }
}
