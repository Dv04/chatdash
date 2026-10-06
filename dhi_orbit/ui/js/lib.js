// Small shared helpers: DOM builder (text only, never innerHTML for data), formatting, icons, storage.

export function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

const SVG = "http://www.w3.org/2000/svg";
const PATHS = {
  ok: "M3 8.5l3 3 7-7",
  unknown: "M8 3v6M8 12.2v.3",
  blocked: "M4 4l8 8M12 4l-8 8",
  focus: "M8 2.5a5.5 5.5 0 1 0 0 11 5.5 5.5 0 0 0 0-11zM8 6a2 2 0 1 0 0 4 2 2 0 0 0 0-4z",
  theme: "M8 2.5a5.5 5.5 0 1 0 0 11V2.5z",
  graph: "M4 4.5a1.5 1.5 0 1 0 0-.01M12 4.5a1.5 1.5 0 1 0 0-.01M8 12a1.5 1.5 0 1 0 0-.01M5.2 5.3l2 5M10.8 5.3l-2 5M5.5 4h5",
  keys: "M2.5 5h11v6h-11zM5 8h.01M8 8h.01M11 8h.01",
  voice: "M8 2.5a2 2 0 0 1 2 2v3a2 2 0 0 1-4 0v-3a2 2 0 0 1 2-2zM4.5 7.5a3.5 3.5 0 0 0 7 0M8 11v2.5",
  search: "M7 3a4 4 0 1 0 0 8 4 4 0 0 0 0-8zM10 10l3.5 3.5",
};
export function icon(name) {
  const s = document.createElementNS(SVG, "svg");
  s.setAttribute("viewBox", "0 0 16 16");
  s.setAttribute("aria-hidden", "true");
  const p = document.createElementNS(SVG, "path");
  p.setAttribute("d", PATHS[name] || "");
  p.setAttribute("fill", "none");
  p.setAttribute("stroke", "currentColor");
  p.setAttribute("stroke-width", "1.6");
  p.setAttribute("stroke-linecap", "round");
  p.setAttribute("stroke-linejoin", "round");
  s.append(p);
  return s;
}

// "2h 14m", "3d 4h", "45s". Returns [big, unit] for the strip's wait column too.
export function age(sec) {
  if (sec == null) return "unknown";
  const [a, b] = ageParts(sec);
  return a + b;
}
export function ageParts(sec) {
  if (sec == null) return ["?", ""];
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return [String(sec), "s"];
  const m = Math.floor(sec / 60);
  if (m < 60) return [String(m), "m"];
  const hh = Math.floor(m / 60);
  if (hh < 48) return [`${hh}h`, m % 60 ? ` ${m % 60}m` : ""];
  const d = Math.floor(hh / 24);
  return [`${d}d`, hh % 24 ? ` ${hh % 24}h` : ""];
}

const CT = new Intl.DateTimeFormat("en-US", { hour: "numeric", minute: "2-digit" });
const CTD = new Intl.DateTimeFormat("en-US", { weekday: "short", hour: "numeric", minute: "2-digit" });
// Time of day in the browser's local time zone, "4:40pm" (never UTC).
export function ct(epoch, withDay = false) {
  if (!epoch) return "unknown";
  const d = new Date(epoch * 1000);
  const sameDay = Math.abs(Date.now() - d) < 20 * 3600e3;
  return (withDay && !sameDay ? CTD : CT).format(d).replace(" AM", "am").replace(" PM", "pm").replace(":00", "");
}

export const store = {
  get(k, dflt) { try { const v = localStorage.getItem("cp2." + k); return v == null ? dflt : JSON.parse(v); } catch { return dflt; } },
  set(k, v) { try { localStorage.setItem("cp2." + k, JSON.stringify(v)); } catch { /* private mode */ } },
};

let toastTimer;
export function toast(msg) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.classList.add("on");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("on"), 3200);
}

// Deterministic split of prose questions "(1) a (2) b" / "1) a 2) b" into parts. Display only.
export function splitParts(text) {
  if (!text) return [];
  const re = /(?:^|\s)\(?(\d{1,2})\)\s+/g;
  const idx = [];
  let m;
  while ((m = re.exec(text))) idx.push({ n: +m[1], at: m.index + m[0].length, start: m.index });
  if (idx.length < 2 || idx[0].n !== 1) return [];
  for (let i = 1; i < idx.length; i++) if (idx[i].n !== idx[i - 1].n + 1) return [];
  return idx.map((p, i) => text.slice(p.at, i + 1 < idx.length ? idx[i + 1].start : undefined).trim().replace(/[;,]$/, ""));
}

// Why a seat has no usage reading, in words. The meter is DHI Orbit's status line (usage_meter.py): "on" means the
// account has not run a chat since it was set, "off" means nothing feeds its limits yet.
export function noReading(s) {
  if (!s) return "no capacity row";
  if (s.usage_meter === "on") return "waiting for the first chat";
  if (s.usage_meter === "off") return "usage not connected (Settings, Accounts)";
  return "no meter reading";
}
// The short label on a seat with no reading (sky and board callouts).
export function unknownLabel(s) {
  if (s && !s.meter_at && s.usage_meter === "on") return "waiting for first chat";
  if (s && !s.meter_at && s.usage_meter === "off") return "usage not connected";
  return "unknown";
}
