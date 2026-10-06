// Answering from the board. Every send goes out at once (as fast as possible; the 5 s Undo window
// is gone), to the asker through the
// contract: decisions/{id}/answer for hook questions, sessions/{id}/reply for prose asks and limits.
import { post } from "./api.js";
import { h } from "./lib.js";

export const DELAY_MS = 0;
export const RESUME_TEXT = "Your limit reset. Continue where you stopped; first say in one line what you were doing.";
const queue = [];          // pending sends (batch "accept all" queues several)
let timer = null;
let hooks = { refresh: null, sent: null };

export function configure(h2) { hooks = { ...hooks, ...h2 }; }

function bar() {
  let b = document.getElementById("undo");
  if (!b) {
    b = h("div", { id: "undo", class: "pop undo", role: "status", "aria-live": "assertive" });
    document.body.append(b);
  }
  return b;
}

export function pathFor(item) {
  return item.kind === "decision"
    ? "decisions/" + encodeURIComponent(item.decision_id) + "/answer"
    : "sessions/" + encodeURIComponent(item.session_id) + "/reply";
}

export function bodyFor(item, body) {
  return item.kind === "decision" ? body : { text: body.text, key: item.key };
}

// Queue one or more sends; all go out together after DELAY_MS unless Undo is pressed.
export function send(item, body, label, delay = DELAY_MS) {
  if (item.excluded || !body) return;
  queue.push({ item, body: bodyFor(item, body), label });
  clearTimeout(timer);
  timer = setTimeout(flush, delay);
  show(delay);
}

function show(delay) {
  const b = bar();
  b.hidden = false;
  if (delay <= 0) {
    const n = queue.length;
    b.replaceChildren(h("div", {}, h("strong", {}, "Sending "), n > 1 ? `${n} answers` : [`"${trim(queue[0].label)}"`, " to ", queue[0].item.title]));
    return;
  }
  const n = queue.length;
  const undo = h("button", { class: "btn", onclick: () => { clearTimeout(timer); queue.length = 0; b.hidden = true; } }, n > 1 ? `Undo all ${n}` : "Undo");
  b.replaceChildren(
    h("div", {}, n > 1 ? h("strong", {}, `${n} answers queued`) : [h("strong", {}, `"${trim(queue[0].label)}"`), " to ", queue[0].item.title]),
    h("div", { class: "hint" }, `Sends in ${delay / 1000} s`),
    h("div", {}, undo, " ", h("button", { class: "btn primary", onclick: () => { clearTimeout(timer); flush(); } }, "Send now")));
  undo.focus();
}

async function flush() {
  const b = bar();
  const batch = queue.splice(0);
  if (!batch.length) return;
  const lines = [];
  for (const q of batch) {
    try {
      const res = await post(pathFor(q.item), q.body);
      lines.push(h("div", {}, h("strong", {}, res.queued ? "Queued" : res.confirmed ? "Delivered" : "Sent, not confirmed"), " ", q.item.title,
        h("span", { class: "hint" }, res.queued ? " (chat is working; sent when it goes idle)" : res.confirmed ? " (read back from the chat)" : " (waiting to read it back)")));
      hooks.sent && hooks.sent(q.item, res);
    } catch (e) {
      lines.push(h("div", {}, h("strong", {}, "Not sent: "), q.item.title, ": ", e.message));
    }
  }
  b.replaceChildren(...lines);
  hooks.refresh && hooks.refresh();
  setTimeout(() => { if (!queue.length) b.hidden = true; }, 6000);
}

function trim(s) { s = String(s || ""); return s.length > 80 ? s.slice(0, 77) + "..." : s; }
