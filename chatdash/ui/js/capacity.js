// B3 capacity: per-seat 5h / 7d gauges with reset countdowns and resume chips, plus a next-24h planner.
// Absence is never green: a missing or since-reset reading is drawn hatched amber and labelled "?".
import { h, ct, age, noReading } from "./lib.js";

const SVGNS = "http://www.w3.org/2000/svg";
function s(tag, attrs = {}, ...kids) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) el.setAttribute(k, v);
  kids.forEach((k) => k != null && el.append(k instanceof Node ? k : document.createTextNode(String(k))));
  return el;
}

// Weekly pace: spending the 7-day limit evenly means pct_per_day (default 100/7 = 14.3%)
// a day. A seat whose remaining share per day until its reset beats that target has spare headroom: use it
// first. Read from the meter only; a reading taken before its own 7d reset is unknown, never guessed.
export function pace(x, now, perDay) {
  const w = x.seven_day || {};
  if (w.pct == null || !w.resets_at) return { state: "unknown", why: "no 7-day reading" };
  if (w.resets_at < now) return { state: "unknown", why: "the 7-day window reset since the last reading" };
  const daysLeft = Math.max((w.resets_at - now) / 86400, 0.04), elapsed = Math.min(7, Math.max(0, 7 - daysLeft));
  const expected = Math.min(100, perDay * elapsed), left = Math.max(0, 100 - w.pct);
  const perDayLeft = left / daysLeft, extra = left - perDay * daysLeft;
  const state = w.pct >= 100 ? "spent" : perDayLeft > perDay * 1.15 ? "use" : perDayLeft < perDay * 0.85 ? "slow" : "on";
  return { state, expected, used: w.pct, left, daysLeft, perDayLeft, extra };
}
function paceLine(p, perDay, x) {
  if (p.state === "unknown") return h("div", { class: "pace unknown" }, `Pace: unknown (${p.why})`);
  const short = p.daysLeft < 1;
  const rate = short ? `${Math.round(p.left)}% left, 7d resets in ${Math.max(1, Math.round(p.daysLeft * 24))} h`
    : `${Math.round(p.perDayLeft)}% a day left for ${p.daysLeft.toFixed(1)} days`;
  const txt = p.state === "spent" ? "7-day limit used up"
    : p.state === "use" ? `Use first: ${rate}${short ? "" : ` (target ${perDay}%)`}, about ${Math.round(p.extra)}% spare`
    : p.state === "slow" ? `Ahead of pace: ${rate}${short ? "" : ` (target ${perDay}%)`}`
    : `On pace: ${rate}`;
  const five = (x.five_hour || {}).pct;
  const fiveFull = five != null && five >= 100 && p.state !== "spent";
  return h("div", { class: "pace " + p.state, title: `By now an even pace would have used about ${Math.round(p.expected)}%; used ${Math.round(p.used)}%.` },
    txt, fiveFull && h("span", { class: "pace-5h" }, `, but the 5h window is full${x.resume_at ? ` until ${ct(x.resume_at)}` : ""}`),
    h("span", { class: "hint" }, ` (should be near ${Math.round(p.expected)}% by now, at ${Math.round(p.used)}%)`));
}

function bar(w, label, now, mark) {
  const known = w.pct != null;
  const cls = !known ? "unk" : w.pct >= 100 ? "full" : w.pct >= 80 ? "near" : "";
  const reset = w.resets_at ? `resets ${ct(w.resets_at, true)} (in ${age(w.resets_at - now)})` : w.since_reset ? "reset since the last reading" : "no reading";
  return h("div", { class: "cap-row" },
    h("span", { class: "cap-k" }, label),
    h("span", { class: "gauge wide " + cls, role: "meter", "aria-valuemin": "0", "aria-valuemax": "100",
      "aria-valuenow": known ? String(Math.round(w.pct)) : null, "aria-label": `${label} used`,
      title: reset }, known && h("i", { style: `width:${Math.min(100, w.pct)}%` }),
      mark != null && h("b", { class: "pace-mark", style: `left:${Math.min(100, mark)}%`, title: `even pace: about ${Math.round(mark)}% by now` })),
    h("span", { class: "cap-v" }, known ? `${Math.round(w.pct)}%` : "?"),
    h("span", { class: "cap-r" }, reset));
}

// Next 7-day reset; a reading whose reset already passed rolls forward a week at a time. Unknown sorts last.
function next7d(x, now) {
  let t = x.seven_day && x.seven_day.resets_at;
  if (!t) return Infinity;
  while (t < now) t += 7 * 86400;
  return t;
}

export function renderCapacity(el, ov, ui) {
  const now = Date.now() / 1000;
  // Nearest 7-day reset first: spend from the seats whose week ends soonest.
  const seats = ov && ov.capacity ? [...ov.capacity.seats].sort((a, b) => next7d(a, now) - next7d(b, now) || a.seat.localeCompare(b.seat)) : null;
  const perDay = (ui.pace && ui.pace.pct_per_day) || 14.3;
  const paces = new Map((seats || []).map((x) => [x.seat, pace(x, now, perDay)]));
  const useFirst = (seats || []).filter((x) => !x.excluded && paces.get(x.seat).state === "use").sort((a, b) => paces.get(b.seat).extra - paces.get(a.seat).extra);
  const head = h("div", { class: "section-head" }, h("h2", {}, "Capacity"),
    h("span", { class: "meta" }, seats ? `nearest 7d reset first; ${seats.filter((x) => x.state === "blocked").length} blocked, ${seats.filter((x) => x.state === "unknown").length} unknown` : ""));
  if (!seats) { el.replaceChildren(head, h("div", { class: "skeleton" })); return; }
  const advice = h("p", { class: "pace-advice" }, useFirst.length
    ? ["Use first (behind the ", perDay, "% a day pace): ", useFirst.map((x, i) => [i ? ", " : "", h("b", {}, x.seat), ` +${Math.round(paces.get(x.seat).extra)}%`,
        (x.five_hour || {}).pct >= 100 ? ` (5h full${x.resume_at ? ` until ${ct(x.resume_at)}` : ""})` : ""])]
    : `No seat is behind the ${perDay}% a day pace.`);
  const rows = seats.map((x) => h("div", { class: "cap-seat " + x.state },
    arcGauge(x),
    h("div", { class: "cap-name" }, h("span", { class: "dot", "aria-hidden": "true" }), h("strong", {}, x.seat),
      h("span", { class: "chip st-" + x.state }, x.state),
      x.excluded && h("span", { class: "chip ro" }, "read-only"),
      x.resume_at && h("span", { class: "chip resume" }, `resumes ${ct(x.resume_at)}`),
      x.queued && x.queued.length > 0 && h("span", { class: "chip" }, `${x.queued.length} stalled`)),
    bar(x.five_hour, "5h", now), bar(x.seven_day, "7d", now, paces.get(x.seat).expected),
    !x.excluded && paceLine(paces.get(x.seat), perDay, x),
    h("div", { class: "hint" }, x.meter_age_min != null ? `meter reading ${x.meter_age_min} min old` : noReading(x))));
  el.replaceChildren(head, advice, h("div", { class: "group cap" }, ...rows), planner(seats, now, ui));
}

// Nebula look only (hidden in the current look by CSS): 7d outer arc, 5h inner arc, and the week left as a figure.
// A window with no usable reading draws no arc and says "?", never a guessed value.
function arcGauge(x) {
  const arc = (pct, r, cls, w) => {
    const c = 2 * Math.PI * r, f = Math.max(0, Math.min(1, pct / 100)) * 0.75;
    return s("circle", { class: cls, cx: 25, cy: 25, r, fill: "none", "stroke-width": w, "stroke-linecap": "round",
      "stroke-dasharray": `${(c * f).toFixed(2)} ${c.toFixed(2)}`, transform: "rotate(135 25 25)" });
  };
  const p7 = (x.seven_day || {}).pct, p5 = (x.five_hour || {}).pct;
  const svg = s("svg", { class: "arc", viewBox: "0 0 50 50", "aria-hidden": "true" },
    arc(100, 21, "tr", 3), p7 != null ? arc(p7, 21, "w7", 3) : null, arc(100, 15, "tr", 2), p5 != null ? arc(p5, 15, "w5", 2) : null);
  return h("div", { class: "cap-arc", "aria-hidden": "true" }, svg,
    h("span", { class: "cap-left" }, p7 == null ? "?" : `${Math.max(0, Math.round(100 - p7))}%`, h("small", {}, "week left")));
}

// Next 24 h: one lane per seat. Tick = 5h reset, diamond = 7d reset, red span = blocked until resume,
// dashed verticals = focus check-ins (local time).
function planner(seats, now, ui) {
  const W = 360, lane = 18, top = 22, H = top + seats.length * lane + 18, left = 52, right = 8;
  const span = 24 * 3600;
  const x = (t) => left + ((t - now) / span) * (W - left - right);
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, class: "planner", role: "img",
    "aria-label": "Next 24 hours: limit resets per seat and check-in windows" });
  // hour grid every 6 h, labelled in local time
  for (let k = 0; k <= 4; k++) {
    const t = now + k * 6 * 3600;
    svg.append(s("line", { x1: x(t), x2: x(t), y1: top - 4, y2: H - 14, class: "pl-grid" }),
      s("text", { x: x(t), y: H - 3, class: "pl-axis", "text-anchor": k === 0 ? "start" : k === 4 ? "end" : "middle" }, k === 0 ? "now" : ct(t)));
  }
  for (const w of (ui.focus && ui.focus.windows) || []) {
    const t = nextLocal(w, now);
    if (t && t - now <= span) svg.append(s("line", { x1: x(t), x2: x(t), y1: top - 10, y2: H - 14, class: "pl-check" }),
      s("text", { x: x(t), y: top - 12, class: "pl-axis", "text-anchor": "middle" }, w));
  }
  seats.forEach((st, i) => {
    const y = top + i * lane + lane / 2;
    const g = s("g", {}, s("text", { x: 0, y: y + 4, class: "pl-label" }, st.seat),
      s("line", { x1: left, x2: W - right, y1: y, y2: y, class: "pl-lane" }));
    if (st.state === "blocked" && st.resume_at) {
      g.append(s("rect", { x: left, y: y - 4, width: Math.max(2, x(Math.min(st.resume_at, now + span)) - left), height: 8, rx: 2, class: "pl-blocked" },
        s("title", {}, `${st.seat} blocked until ${ct(st.resume_at)}`)));
    }
    const f = st.five_hour.resets_at, d7 = st.seven_day.resets_at;
    if (f && f > now && f - now <= span) g.append(s("rect", { x: x(f) - 1.5, y: y - 6, width: 3, height: 12, rx: 1.5, class: "pl-reset" },
      s("title", {}, `${st.seat} 5h window resets ${ct(f)}`)));
    if (d7 && d7 > now && d7 - now <= span) g.append(s("path", { d: `M${x(d7)} ${y - 6} l5 6 l-5 6 l-5 -6 z`, class: "pl-week" },
      s("title", {}, `${st.seat} 7-day window resets ${ct(d7, true)}`)));
    if (st.state === "unknown") g.append(s("text", { x: W - right, y: y + 4, class: "pl-unk", "text-anchor": "end" }, "unknown"));
    svg.append(g);
  });
  const legend = h("div", { class: "pl-legend" },
    h("span", {}, h("i", { class: "lg reset" }), "5h reset"), h("span", {}, h("i", { class: "lg week" }), "7d reset"),
    h("span", {}, h("i", { class: "lg blocked" }), "blocked"), h("span", {}, h("i", { class: "lg check" }), "check-in"));
  return h("figure", { class: "group plan" }, h("figcaption", {}, "Next 24 hours"), svg, legend);
}

const CTF = new Intl.DateTimeFormat("en-US", { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
// Next epoch at which local wall time equals "HH:MM".
export function nextLocal(hhmm, now) {
  const [H, M] = hhmm.split(":").map(Number);
  const [h0, m0] = CTF.format(new Date(now * 1000)).split(":").map(Number);
  let delta = (H * 60 + M) - (h0 * 60 + m0);
  if (delta <= 0) delta += 24 * 60;
  return Math.floor(now / 60) * 60 + delta * 60;
}
