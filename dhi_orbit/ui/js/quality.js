// Quality settings: how much the renderers draw, saved per browser (Settings > Quality). The defaults are exactly what
// Orbit drew before this existed, so nobody sees a change until they move a slider. Each value is an index into a short
// list of steps; the renderers read the resolved numbers from Q and Q.on(fn) tells them what changed (no reload).
import { store } from "./lib.js";

// key: [label, steps, default step index, what it does]. A step is [shown, value].
export const KNOBS = {
  fps: ["Frame rate limit", [["15", 15], ["30", 30], ["60", 60], ["Max", 0]], 3,
    "Caps how often the background and Sky are redrawn. Lower saves battery and GPU; Max follows your screen."],
  res: ["Render resolution", [["50%", 0.5], ["75%", 0.75], ["100%", 1], ["150%", 1.5], ["200%", 2], ["300%", 3]], 4,
    "Pixels drawn per screen pixel for the background, Sky and Graph. Above 100% only sharpens on high-density screens."],
  detail: ["Nebula detail", [["50%", 0.5], ["75%", 0.75], ["100%", 1], ["150%", 1.5], ["200%", 2]], 2,
    "Resolution of the gas clouds. The biggest GPU cost: lower it first on a slow machine."],
  density: ["Particles", [["None", 0], ["50%", 0.5], ["100%", 1], ["200%", 2], ["400%", 4]], 2,
    "How many stars and dust motes are drawn."],
  bloom: ["Glow", [["Off", 0], ["Soft", 0.6], ["Normal", 1], ["Strong", 1.5]], 2,
    "The halo around bright cores. Off also skips its render passes."],
  blur: ["Glass blur", [["Off", 0], ["Low", 0.5], ["Normal", 1], ["High", 1.5]], 2,
    "Blur behind the glass panels. Off makes the panels more opaque so text stays readable."],
};
const BOOLS = { live: ["Animate background", true, "Off draws one still frame instead of a moving sky."],
  anim: ["Interface animations", true, "Off removes slide, fade and settle effects (same as the system reduced-motion setting)."] };
export const TOGGLES = BOOLS;

// One click sets every slider. "Balanced" is the shipped default.
export const PRESETS = {
  Low: { fps: 1, res: 2, detail: 0, density: 1, bloom: 0, blur: 0, live: true, anim: false },
  Balanced: Object.fromEntries([...Object.entries(KNOBS).map(([k, v]) => [k, v[2]]), ...Object.entries(BOOLS).map(([k, v]) => [k, v[1]])]),
  High: { fps: 3, res: 4, detail: 3, density: 3, bloom: 3, blur: 3, live: true, anim: true },
  Ultra: { fps: 3, res: 5, detail: 4, density: 4, bloom: 3, blur: 3, live: true, anim: true },
};

const listeners = new Set();
let cur = null;

function clean(raw) {
  const o = {};
  for (const [k, [, steps, d]] of Object.entries(KNOBS)) {
    const i = raw && Number.isInteger(raw[k]) ? raw[k] : d;
    o[k] = i >= 0 && i < steps.length ? i : d;
  }
  for (const [k, [, d]] of Object.entries(BOOLS)) o[k] = raw && typeof raw[k] === "boolean" ? raw[k] : d;
  return o;
}

function apply() {
  const root = document.documentElement;
  const blur = KNOBS.blur[1][cur.blur][1];
  root.style.setProperty("--q-blur", String(blur));
  root.dataset.qBlur = blur === 0 ? "off" : "on";
  root.dataset.qAnim = cur.anim ? "on" : "off";
}

export const Q = {
  get idx() { if (!cur) { cur = clean(store.get("quality", null)); apply(); } return cur; },
  val(k) { return KNOBS[k][1][this.idx[k]][1]; },
  get fps() { return this.val("fps"); },
  get res() { return this.val("res"); },
  get detail() { return this.val("detail"); },
  get density() { return this.val("density"); },
  get bloom() { return this.val("bloom"); },
  get blur() { return this.val("blur"); },
  get live() { return this.idx.live; },
  get anim() { return this.idx.anim; },
  // Minimum gap between painted frames in ms; 0 = every display frame. The 1 ms slack keeps a 30 cap on a 60 Hz screen at 30.
  get frameMs() { return this.fps ? 1000 / this.fps - 1 : 0; },
  set(patch) {
    const before = this.idx, next = clean({ ...before, ...patch });
    const changed = Object.keys(next).filter((k) => next[k] !== before[k]);
    if (!changed.length) return;
    cur = next; store.set("quality", cur); apply();
    for (const fn of listeners) { try { fn(changed); } catch { /* one listener's error must not stop the others */ } }
  },
  reset() { this.set(PRESETS.Balanced); },
  on(fn) { listeners.add(fn); return () => listeners.delete(fn); },
  // The preset these values match, or "Custom".
  preset() {
    const c = this.idx;
    for (const [name, p] of Object.entries(PRESETS)) if (Object.keys(p).every((k) => p[k] === c[k])) return name;
    return "Custom";
  },
};
Q.idx;    // read and apply the saved blur and animation attributes before the first paint
