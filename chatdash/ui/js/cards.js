// B2 decision cards: the interactive body of a NEEDS YOU strip. Drafts live in ui.drafts so the 5 s
// refresh never loses a selection or half-typed text.
import { h, ct, splitParts, toast } from "./lib.js";
import { post } from "./api.js";
import { RESUME_TEXT } from "./answer.js";

export function draftOf(ui, id) {
  if (!ui.drafts.has(id)) ui.drafts.set(id, { sel: {}, text: {}, reply: "" });
  return ui.drafts.get(id);
}

// ------------------------------------------------------------------ what a strip asks
// decision: parts from the hook (AskUserQuestion), 1 to 4 questions with options.
// blocked/dialog: prose; numbered "(1) ... (2) ..." becomes parts with free-text answers.
export function partsOf(x) {
  if (x.kind === "decision") {
    const parts = x.parts && x.parts.length ? x.parts
      : [{ question: x.text, header: "", multi: false, options: x.options || [], recommended: x.recommended }];
    return parts.map((p) => ({ ...p, free: false }));
  }
  if ((x.kind === "blocked" || x.kind === "dialog") && x.suggested_parts && x.suggested_parts.length) {
    // shadow suggestion (never sent by itself): options to click; the answers still go out as one reply
    return x.suggested_parts.map((p) => ({ question: p.question, free: true, suggested: true,
      options: (p.options || []).map((label, k) => ({ id: String(k + 1), label: String(label) })),
      recommended: p.recommended ? String((p.options || []).indexOf(p.recommended) + 1) : null }));
  }
  if (x.kind === "blocked" || x.kind === "dialog") {
    const ps = splitParts(x.text);
    if (ps.length > 1) return ps.map((q) => ({ question: q, options: [], free: true }));
  }
  return [];
}

export function complete(x, d) {
  const parts = partsOf(x);
  if (!parts.length) return false;
  return parts.every((p, i) => (d.sel[i] && d.sel[i].length) || (d.text[i] || "").trim());
}

// The request body for a complete draft (null if incomplete).
export function bodyFor(x, d) {
  const parts = partsOf(x);
  if (!complete(x, d)) return null;
  if (x.kind === "decision") {
    const answers = {};
    parts.forEach((p, i) => {
      const t = (d.text[i] || "").trim();
      answers[i] = t ? { text: t } : (p.multi ? d.sel[i] : d.sel[i][0]);
    });
    return { answers };
  }
  // prose parts: one reply in the chat's own numbering
  const ans = (p, i) => (d.text[i] || "").trim() || (d.sel[i] || []).map((id) => (p.options.find((o) => o.id === id) || {}).label).join(", ");
  if (parts.length === 1) return { text: ans(parts[0], 0) };
  return { text: parts.map((p, i) => `${i + 1}: ${ans(p, i)}`).join("\n") };
}

function label(x, d) {
  const parts = partsOf(x);
  return parts.map((p, i) => {
    const t = (d.text[i] || "").trim();
    if (t) return t;
    return (d.sel[i] || []).map((id) => (p.options.find((o) => o.id === id) || {}).label).join(", ");
  }).join(" / ");
}

// ------------------------------------------------------------------ render
export function cardBody(x, ui, onChange) {
  const d = draftOf(ui, x.id);
  const parts = partsOf(x);
  const ro = x.excluded;
  const wrap = h("div", { class: "card" });
  if (x.delivery === "pending") {
    wrap.append(h("div", { class: "receipt none" }, "Answered: sent, not confirmed yet (waiting for the chat's hook to read it)"));
    return wrap;
  }
  if (x.screen && (x.screen.options || []).length) {
    wrap.append(screenView(x, d, ro, ui));
    return wrap;
  }
  if (parts.length) {
    const n = parts.length;
    const answered = parts.filter((p, i) => (d.sel[i] && d.sel[i].length) || (d.text[i] || "").trim()).length;
    if (parts[0].suggested) wrap.append(h("div", { class: "hint" }, "Suggested options (shadow, unverified): pick or type; nothing is sent until you press Send."));
    // Typing in an answer field updates only the Send button and the count in place; rebuilding the strip (and the
    // whole board) on every keystroke made typing lag and, on Sky, moved focus away from the field.
    let send = null, prog = null, note = null;
    const counts = () => parts.filter((p, i) => (d.sel[i] && d.sel[i].length) || (d.text[i] || "").trim()).length;
    const changed = (soft) => {
      if (!soft) return onChange();
      const k = counts();
      if (send) send.disabled = k < n;
      if (prog) prog.textContent = `${k} of ${n} answered`;
      if (note) note.hidden = k >= n;
    };
    parts.forEach((p, i) => wrap.append(partView(x, p, i, n, d, ro, changed)));
    if (!ro) {
      send = h("button", { class: "btn primary", disabled: answered < n,
        onclick: () => ui.send(x, bodyFor(x, d), label(x, d)) }, n > 1 ? `Send all ${n} answers` : "Send");
      prog = n > 1 ? h("span", { class: "progress", "aria-live": "polite" }, `${answered} of ${n} answered`) : null;
      note = n > 1 ? h("span", { class: "hint", hidden: answered >= n }, "Nothing is sent until every part has an answer.") : null;
      wrap.append(h("div", { class: "sendrow" }, send, prog, note));
    }
  } else if (x.kind === "limit" && !ro) {
    wrap.append(h("div", { class: "ask" }, x.text),
      h("div", { class: "opts" }, h("button", { class: "opt rec", onclick: () => ui.send(x, { text: RESUME_TEXT }, "Resume") },
        h("kbd", {}, "1"), "Resume now")));
  } else {
    wrap.append(h("div", { class: "ask" }, x.text || "(no question text)"));
    if (x.suggested && !ro) {
      wrap.append(h("div", { class: "opts" }, h("button", { class: "opt rec", title: "Claude Code's own suggested reply",
        onclick: () => ui.send(x, { text: x.suggested }, x.suggested) }, h("kbd", {}, "1"), x.suggested)));
    }
  }
  if (!ro && x.draft && x.draft.text && x.kind !== "decision") {
    wrap.append(h("details", { class: "evidence draft" },
      h("summary", {}, `Draft (${x.draft.class === "retrieved" ? "how you replied to similar messages before" : x.draft.class}, never sent by itself)`),
      h("p", { class: "final" }, x.draft.text),
      (x.draft.pairs || []).slice(1).map((p) => h("p", { class: "hint" }, "Also: ", p.reply)),
      h("button", { class: "btn", onclick: () => ui.useDraft(x, x.draft) }, "Use as reply")));
  }
  if (!ro && x.kind !== "decision") wrap.append(replyBox(x, d, ui));
  return wrap;
}

// The chat's own dialog as read from its screen: the same options, same numbers, same order as the terminal
// (Yes / Yes, and always allow ... / No, or a question's options plus "Type something." and "Chat about this").
// Nothing here is generated; the server re-reads the screen and refuses if the option changed.
function screenView(x, d, ro, ui) {
  const s = x.screen;
  const pick = async (o, text) => {
    try {
      await post(`sessions/${encodeURIComponent(x.session_id)}/dialog`, { n: o ? o.n : 0, label: o ? o.label : "", text: text || "" });
      toast(o ? `Sent ${o.n}. ${o.label}` : "Sent Esc"); ui.refresh();
    } catch (e) { toast(`Not sent: ${e.message}`); }
  };
  const free = (o) => {
    const inp = h("input", { type: "text", class: "free", value: d.text.screen || "", "data-fk": `${x.id}:screen`, disabled: ro,
      "aria-label": o.label, placeholder: `${o.n}. ${o.label}`,
      oninput: (e) => { d.text.screen = e.target.value; },
      onkeydown: (e) => { if (e.key === "Enter" && (d.text.screen || "").trim()) pick(o, d.text.screen.trim()); } });
    return h("div", { class: "replyrow" }, inp,
      h("button", { class: "btn", disabled: ro, onclick: () => (d.text.screen || "").trim() && pick(o, d.text.screen.trim()) }, "Send"));
  };
  return h("div", { class: "part screen", role: "group", "aria-label": "Options on the chat's screen" },
    s.tabs && h("div", { class: "hint" }, s.tabs),
    h("div", { class: "legend ask" }, s.question || x.text || ""),
    s.options.map((o) => o.free ? free(o) : h("div", { class: "opts" },
      h("button", { class: "opt", disabled: ro, title: o.desc || "", onclick: () => pick(o) },
        h("kbd", {}, o.n), o.label, o.desc && h("span", { class: "desc" }, o.desc)))),
    h("div", { class: "opts" }, h("button", { class: "btn ghost", disabled: ro, onclick: () => pick(null) }, "Esc (cancel)")),
    h("div", { class: "hint" }, "Exactly what the chat's terminal shows; picking one presses that option there."));
}

function partView(x, p, i, n, d, ro, onChange) {
  const sel = d.sel[i] || [];
  const box = h("div", { class: "part", role: "group", "aria-label": n > 1 ? `Part ${i + 1} of ${n}` : "Answer" });
  if (n > 1 || x.kind !== "decision") box.append(h("div", { class: "legend" }, n > 1 ? `${i + 1}. ` : "", p.header ? `${p.header}: ` : "", p.question));
  else box.append(h("div", { class: "legend ask" }, p.question));
  if (p.options && p.options.length) {
    box.append(h("div", { class: "opts", role: p.multi ? "group" : "radiogroup" }, p.options.map((o, k) => {
      const on = sel.includes(o.id);
      return h("button", { class: "opt" + (o.id === p.recommended ? " rec" : "") + (on ? " on" : ""),
        "aria-pressed": String(on), disabled: ro, title: o.desc || "",
        onclick: () => {
          if (p.multi) d.sel[i] = on ? sel.filter((s) => s !== o.id) : [...sel, o.id];
          else d.sel[i] = on ? [] : [o.id];
          if (d.sel[i].length) d.text[i] = "";
          onChange();
        } }, h("kbd", {}, k + 1), o.label.replace(/\s*\(recommended\)\s*$/i, ""), o.desc && h("span", { class: "desc" }, o.desc));
    })));
  }
  if (!ro) {
    const inp = h("input", { type: "text", class: "free", value: d.text[i] || "", "data-fk": `${x.id}:p${i}`, "aria-label": p.free ? `Answer to part ${i + 1}` : "Other answer",
      placeholder: p.free ? "Your answer" : "Other (type an answer instead)",
      oninput: (e) => {
        d.text[i] = e.target.value;
        const unpick = e.target.value.trim() && (d.sel[i] || []).length;   // typing replaces a picked option:
        if (unpick) d.sel[i] = [];                                           // rebuild once so it shows unpicked
        onChange(!unpick);
      } });
    box.append(inp);
  }
  return box;
}

function replyBox(x, d, ui) {
  const ta = h("textarea", { class: "reply", rows: "2", "data-fk": `${x.id}:reply`, placeholder: "Reply to this chat (Cmd+Enter sends)", "aria-label": "Reply",
    oninput: (e) => { d.reply = e.target.value; const m = ui.dirty; m && m(x.id); },
    onkeydown: (e) => {
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && d.reply.trim()) { e.preventDefault(); ui.send(x, { text: d.reply.trim() }, d.reply.trim()); }
    } });
  ta.value = d.reply;
  return h("div", { class: "replyrow" }, ta,
    h("button", { class: "btn", onclick: () => d.reply.trim() && ui.send(x, { text: d.reply.trim() }, d.reply.trim()) }, "Reply"));
}

export function timeoutLine(x) {
  if (x.kind === "decision") {
    if (x.held && x.on_timeout === "default" && x.timeout_at) return `Low risk: if unanswered by ${ct(x.timeout_at)}, the recommended option is taken.`;
    if (x.held && x.timeout_at) return `The chat is held for your answer until ${ct(x.timeout_at)}, then it shows its own dialog; this card stays answerable.`;
    if (x.mode === "dry-run") return "Dry-run: the chat shows its own dialog now; answering here types into that dialog.";
    return "The chat shows its own dialog; answering here types into it.";
  }
  if (x.kind === "limit") return "Limit-resume sends the context line itself once its mode is on (dry-run now); Resume now sends it by hand.";
  if (x.kind === "dialog") return "The chat is showing a dialog and waits until you answer.";
  return "The chat waits for your reply; nothing is sent on its behalf.";
}

// ------------------------------------------------------------------ batch: accept all low-risk recommended
export function lowRiskRecommended(items) {
  return items.filter((x) => x.kind === "decision" && x.risk === "low" && !x.excluded && x.delivery !== "pending" &&
    partsOf(x).every((p) => p.recommended));
}
export function recommendedBody(x) {
  const answers = {};
  partsOf(x).forEach((p, i) => { answers[i] = p.multi ? [p.recommended] : p.recommended; });
  return { answers };
}
