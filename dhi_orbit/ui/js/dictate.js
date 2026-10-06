// Speech to text into any reply box: a mic button next to the textarea. Words appear live (interim text is
// shown in the box and replaced by the final words), and each change fires the box's own input handler, so the
// reply state, autosave and Cmd+Enter keep working. One dictation at a time; it follows its box across a
// re-render through data-fk. On-device recognition when the browser offers it, else the browser's service
// (the button title says which, once known). Nothing is sent by voice: the person still presses Send.
import { h, icon, toast } from "./lib.js";

const SR = window.SpeechRecognition || window.webkitSpeechRecognition;

// On-device recognition (Chrome): available() says whether the language pack is there; install() fetches it.
// Anything else (Safari, older Chrome) has no on-device switch, so recognition uses the browser's service.
export async function localReady() {
  try {
    if (!SR || typeof SR.available !== "function") return false;
    const st = await SR.available({ langs: ["en-US"], processLocally: true });
    if (st === "available") return true;
    if ((st === "downloadable" || st === "downloading") && typeof SR.install === "function") return !!(await SR.install({ langs: ["en-US"], processLocally: true }));
  } catch { /* fall through */ }
  return false;
}

let cur = null;           // { rec, key, ta, base, btns:Set }
let local = null;

// Phrases arrive without a reliable leading space (Safari never sends one): join with exactly one.
function join(a, b) {
  if (!a || !b) return a + b;
  return /\s$/.test(a) || /^\s/.test(b) ? a + b : a + " " + b;
}

function boxOf(c) {
  if (c.ta.isConnected) return c.ta;
  const t = c.key && document.querySelector(`textarea[data-fk="${CSS.escape(c.key)}"]`);
  if (t) c.ta = t;
  return t || c.ta;
}

function put(c, text) {
  const ta = boxOf(c);
  ta.value = text;
  ta.dispatchEvent(new Event("input", { bubbles: true }));
}

function mark(on) {
  if (!cur) return;
  for (const b of cur.btns) {
    b.classList.toggle("on", on);
    b.setAttribute("aria-pressed", String(on));
    b.title = on ? "Stop dictation" : "Dictate (speech to text)";
    if (on) b.title += local ? ", on this device" : ", through the browser's speech service";
  }
}

export function stopDictation() {
  if (!cur) return;
  const c = cur;
  mark(false);
  cur = null;
  try { c.rec.stop(); } catch { /* already stopped */ }
}

async function start(ta, btn) {
  if (!SR) { toast("This browser has no speech recognition. Chrome and Safari do."); return; }
  if (local == null) local = await localReady();
  const rec = new SR();
  rec.lang = "en-US";
  rec.continuous = true;
  rec.interimResults = true;
  if (local && "processLocally" in rec) { try { rec.processLocally = true; } catch { local = false; } }
  const sep = ta.value && !/\s$/.test(ta.value) ? " " : "";
  const c = { rec, key: ta.dataset.fk || null, ta, base: ta.value + sep, fin: "", btns: new Set([btn]) };
  cur = c;
  rec.onresult = (e) => {
    if (cur !== c) return;
    let interim = "";
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const t = e.results[i][0].transcript;
      if (e.results[i].isFinal) c.fin = join(c.fin, t); else interim += t;
    }
    put(c, join(join(c.base, c.fin), interim));
  };
  rec.onerror = (e) => {
    if (cur !== c) return;
    const why = { "not-allowed": "microphone access is blocked for this page (browser site settings)", "audio-capture": "no microphone found",
      network: "the speech service could not be reached", "service-not-allowed": "speech recognition is not allowed here" }[e.error];
    if (why) { toast("Dictation stopped: " + why); stopDictation(); }
  };
  rec.onend = () => {
    if (cur !== c) return;
    c.base = join(c.base, c.fin); c.fin = "";
    put(c, c.base);                                   // drop any interim words that never became final
    try { rec.start(); } catch { stopDictation(); }   // a silence pause ends recognition: keep listening until stopped
  };
  try { rec.start(); } catch { cur = null; toast("Dictation could not start"); return; }
  mark(true);
  boxOf(c).focus();
}

// A mic button for one textarea. Adopt an existing dictation if this box is a re-render of the one being dictated.
export function micFor(ta) {
  const btn = h("button", { type: "button", class: "btn ghost mic", "aria-label": "Dictate", "aria-pressed": "false",
    title: SR ? "Dictate (speech to text)" : "No speech recognition in this browser",
    onclick: (e) => { e.preventDefault(); cur && (cur.btns.has(btn) || cur.ta === ta) ? stopDictation() : (stopDictation(), start(ta, btn)); } },
    icon("voice"));
  if (cur && ta.dataset.fk && cur.key === ta.dataset.fk) { cur.btns.add(btn); cur.ta = ta; queueMicrotask(() => mark(true)); }
  if (!SR) btn.disabled = true;
  return btn;
}
