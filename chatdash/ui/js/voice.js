// B9 voice: briefing (read aloud), hands-free decisions, and conversation mode (speech -> /intent -> read
// back -> spoken yes -> run). Speech out uses local system voices only. Speech in uses the browser's
// SpeechRecognition with processLocally where supported; where it is not, the panel says audio may leave the Mac.
// Nothing destructive runs without a spoken (or tapped) yes; permission prompts are never answered by voice.
import { h, age, ct, toast } from "./lib.js";
import { post } from "./api.js";
import { describe } from "./palette.js";
import { splitNeeds } from "./board.js";

const SR = window.SpeechRecognition || window.webkitSpeechRecognition;

// On-device recognition (Chrome): available() says whether the language pack is there; install() fetches it.
// Anything else (Safari, older Chrome) has no on-device switch, so recognition uses the browser's service.
async function localReady() {
  try {
    if (!SR || typeof SR.available !== "function") return false;
    const st = await SR.available({ langs: ["en-US"], processLocally: true });
    if (st === "available") return true;
    if ((st === "downloadable" || st === "downloading") && typeof SR.install === "function") return !!(await SR.install({ langs: ["en-US"], processLocally: true }));
  } catch { /* fall through */ }
  return false;
}

export class Voice {
  constructor(ui) {
    this.ui = ui;
    this.panel = document.body.appendChild(h("div", { class: "pop voice", role: "dialog", "aria-label": "Voice", hidden: true }));
    this.on = false;
  }

  voices() {
    const all = speechSynthesis.getVoices();
    return all.filter((v) => v.localService && /^en/i.test(v.lang));
  }

  say(text) {
    return new Promise((res) => {
      if (!("speechSynthesis" in window)) return res();
      const u = new SpeechSynthesisUtterance(text);
      const v = this.voices()[0];
      if (v) u.voice = v;
      u.rate = 1.05;
      u.onend = u.onerror = () => res();
      speechSynthesis.cancel();
      speechSynthesis.speak(u);
      this.status(text);
    });
  }

  // Listen for one utterance. Resolves the text, or null with this.lastError set. Recognition that ends
  // early (silence, an aborted start) is restarted until `ms` has passed, so a prompt is never answered
  // by an instant "did not catch that". On-device recognition is tried first; if the browser cannot do
  // it, it falls back to the browser's speech service and the panel says audio may leave the Mac.
  async listen(ms = 9000) {
    if (!SR) { this.lastError = "no speech recognition in this browser"; return null; }
    await new Promise((r) => setTimeout(r, 300));          // let the spoken prompt's tail clear the speakers
    const until = Date.now() + ms;
    this.lastError = null;
    if (this.local == null) this.local = await localReady();
    let restarts = 0;
    while (this.on && Date.now() < until) {
      const res = await this.once(until - Date.now());
      if (res.text) return res.text;
      const err = res.error;
      if (err === "language-not-supported" || err === "service-not-allowed" && this.local) {
        if (this.local) { this.local = false; this.lastError = `on-device recognition unavailable (${err}); using the browser's speech service`; continue; }
      }
      if (err === "not-allowed") { this.lastError = "microphone access is blocked for this page (browser site settings)"; return null; }
      if (err === "audio-capture") { this.lastError = "no microphone found"; return null; }
      if (err === "network") { this.lastError = "the speech service could not be reached"; return null; }
      if (err && err !== "no-speech" && err !== "aborted") { this.lastError = err; return null; }
      if (++restarts > 100) break;                           // ended early with no words: listen again,
      await new Promise((r) => setTimeout(r, 250));          // after a short pause (no hot loop), until `ms` is up
    }
    this.lastError = this.lastError || "heard nothing";
    return null;
  }

  once(ms) {
    return new Promise((res) => {
      const r = new SR();
      r.lang = "en-US"; r.interimResults = true; r.maxAlternatives = 1; r.continuous = true;
      if (this.local && "processLocally" in r) { try { r.processLocally = true; } catch { this.local = false; } }
      let done = false, error = null, text = "";
      const end = () => { if (done) return; done = true; clearTimeout(t); try { r.stop(); } catch { /* already */ } res({ text: text.trim() || null, error }); };
      r.onresult = (e) => {
        let interim = "";
        for (let i = e.resultIndex; i < e.results.length; i++) {
          if (e.results[i].isFinal) { text += e.results[i][0].transcript; end(); return; }
          interim += e.results[i][0].transcript;
        }
        this.status("Heard: " + interim, true);
      };
      r.onerror = (e) => { error = e.error || "error"; end(); };
      r.onend = () => end();
      const t = setTimeout(end, Math.max(500, ms));
      try { r.start(); this.status("Listening, speak now", true); } catch (e) { error = "aborted"; end(); }
    });
  }

  status(t, listening = false) {
    this.panel.hidden = false;
    this.panel.replaceChildren(h("h3", {}, "Voice"), h("p", { class: listening ? "final listening" : "final" }, t),
      this.lastError && !listening && h("p", { class: "err" }, "Speech input: " + this.lastError),
      SR ? h("p", { class: "hint" }, this.local ? "Speech recognition runs on this device." : "This browser may send microphone audio to its speech service; type in the palette (Cmd+K) to avoid that.")
        : h("p", { class: "hint" }, "No speech recognition in this browser: briefing and read-out work, answers are tapped."),
      h("button", { class: "btn", onclick: () => this.stop() }, "Stop voice"));
  }

  stop(keepPanel = false) { this.on = false; speechSynthesis.cancel(); if (!keepPanel) this.panel.hidden = true; }

  summary() {
    const ov = this.ui.overview();
    if (!ov) return "No data yet.";
    const parts = [];
    if (ov.health !== "ok") parts.push("Warning: some data is unknown right now.");
    const { now: workable, parked } = splitNeeds(ov);
    const n = workable.length, lw = workable.reduce((m, x) => (!m || (x.seconds || 0) > (m.seconds || 0) ? x : m), null);
    parts.push(n ? `${n} ${n === 1 ? "thing needs" : "things need"} you.` : "Nothing needs you.");
    if (parked.length) parts.push(`${parked.length} more parked on a seat at its limit.`);
    if (lw) parts.push(`Waiting longest: ${lw.title}, ${spoken(age(lw.seconds))}.`);
    const seats = ov.capacity.seats.filter((s) => !s.excluded);
    const blocked = seats.filter((s) => s.state === "blocked");
    const near = seats.filter((s) => s.state === "near");
    if (blocked.length) parts.push(`Blocked: ${blocked.map((s) => s.seat + (s.resume_at ? ` until ${ct(s.resume_at)}` : "")).join(", ")}.`);
    if (near.length) parts.push(`Near the limit: ${near.map((s) => s.seat).join(", ")}.`);
    if (ov.proposals && ov.proposals.length) parts.push(`${ov.proposals.length} proposals wait for apply or skip.`);
    return parts.join(" ");
  }

  async briefing() { this.on = true; await this.say(this.summary()); }

  readItem(x) {
    const opts = (x.parts && x.parts.length === 1 ? x.parts[0].options : x.options || []).map((o, i) => `Option ${i + 1}: ${o.label.replace(/\(recommended\)/i, ", recommended")}.`);
    return `${x.title}. ${x.text || ""} ${opts.join(" ")} ${x.risk === "high" ? "This is high risk." : ""}`;
  }

  // Hands-free: read each answerable decision; accept a spoken option number, then confirm.
  async handsFree() {
    this.on = true;
    const items = (this.ui.overview()?.needs_you || []).filter((x) => !x.excluded && x.kind === "decision" && (x.parts || []).length <= 1);
    if (!items.length) return this.say("No decisions to answer by voice right now. " + this.summary());
    for (const x of items) {
      if (!this.on) return;
      await this.say(this.readItem(x) + " Say an option number, skip, or repeat.");
      let heard = await this.listen();
      if (!heard && this.lastError && this.lastError !== "heard nothing") { await this.say("Speech input is not working: " + this.lastError + ". Voice stopped."); return this.stop(true); }
      if (heard && /repeat/i.test(heard)) { await this.say(this.readItem(x)); heard = await this.listen(); }
      if (!heard || /skip|next|later/i.test(heard)) continue;
      const r = await post("intent", { text: heard, model: false }).catch(() => null);
      const n = r && r.cmd === "answer" ? r.option : null;
      const opts = (x.parts && x.parts[0] ? x.parts[0].options : x.options) || [];
      if (!n || !opts[n - 1]) { await this.say(`I heard "${heard}", which is not an option. Skipping.`); continue; }
      await this.say(`Option ${n}: ${opts[n - 1].label}. Say yes to send.`);
      const yes = await this.listen(5000);
      if (yes && /^\s*(yes|yeah|yep|send|confirm)/i.test(yes)) {
        this.ui.send(x, x.parts && x.parts.length ? { answers: { 0: opts[n - 1].id } } : { option_id: opts[n - 1].id }, opts[n - 1].label);
        await this.say("Sent.");
      } else await this.say("Not sent.");
    }
    await this.say("That was the last one.");
  }

  // Conversation mode: any sentence -> intent -> read back -> yes -> run.
  async converse() {
    this.on = true;
    let misses = 0;
    await this.say("Listening. Say what you want, for example: what needs me, or reply continue to papers.");
    while (this.on) {
      const heard = await this.listen(9000);
      if (!this.on) return;
      if (!heard) {
        misses = (misses || 0) + 1;
        if (this.lastError && this.lastError !== "heard nothing") { await this.say("Speech input is not working: " + this.lastError + ". Voice stopped."); return this.stop(true); }
        if (misses >= 2) { await this.say("I heard nothing twice. Voice stopped; press Talk to start again."); return this.stop(true); }
        await this.say("I did not hear anything. Go ahead.");
        continue;
      }
      misses = 0;
      if (/^\s*(stop voice|goodbye|that's all|thats all)/i.test(heard)) { await this.say("Ending voice."); return this.stop(); }
      const r = await post("intent", { text: heard }).catch((e) => ({ cmd: null, error: e.message }));
      if (!r.cmd) { await this.say(`I heard "${heard}" but it is not a command I know.`); continue; }
      if (r.cmd === "needs_you" || r.cmd === "status") { await this.say(this.summary()); continue; }
      if (r.confirm || r.source === "model") {
        await this.say(`${describe(r)}. Say yes to do it.`);
        const yes = await this.listen(5000);
        if (!(yes && /^\s*(yes|yeah|yep|do it|confirm|send)/i.test(yes))) { await this.say("Cancelled."); continue; }
      }
      const said = await this.ui.runIntent(r);
      await this.say(said || "Done.");
    }
  }
}

function spoken(a) { return a.replace(/(\d+)h/, "$1 hours").replace(/(\d+)m\b/, "$1 minutes").replace(/(\d+)d/, "$1 days").replace(/(\d+)s\b/, "$1 seconds"); }
export { toast };
