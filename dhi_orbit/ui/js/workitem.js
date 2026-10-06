// B6 work item page (#/work/<id>) and the proposals list (Apply / Skip cards with a diff).
import { h, ct, age, toast } from "./lib.js";
import { seatsAtLimit, limitWord } from "./limits.js";
import { get, post, put } from "./api.js";

export function diffView(text) {
  const lines = (text || "").split("\n").filter((l) => !l.startsWith("---") && !l.startsWith("+++"));
  return h("pre", { class: "diff", "aria-label": "Proposed change" }, lines.map((l) =>
    h("span", { class: l.startsWith("+") ? "add" : l.startsWith("-") ? "del" : l.startsWith("@@") ? "hunk" : "" }, l + "\n")));
}

const KIND_NOTE = {
  state_doc: "Regenerated from receipts, decisions and finals; your text outside the cp block is kept.",
  handoff: "Confirm sends the chat a handoff prompt, checks the note landed in the state doc, then stops the chat (dry-run until the handoff mode is on).",
  rule: "Apply only records your OK. No CLAUDE.md is edited by cp.",
};

export function proposalCard(p, onDone) {
  const body = h("div", { class: "prop-body" }, h("p", { class: "hint" }, KIND_NOTE[p.proposal_kind || p.kind] || ""));
  let loaded = false;
  const details = h("details", { class: "evidence", ontoggle: async () => {
    if (!details.open || loaded) return;
    loaded = true;
    try {
      const full = await get("proposals/" + (p.proposal_id || p.id));
      body.append(full.diff ? diffView(full.diff) : h("p", { class: "final" }, full.why || ""), full.detail ? h("p", { class: "hint" }, full.detail) : "");
    } catch (e) { body.append(h("p", { class: "receipt none" }, `Could not load: ${e.message}`)); }
  } }, h("summary", {}, (p.proposal_kind || p.kind) === "handoff" ? "What happens" : "Show the change"), body);
  const act = async (a) => {
    try {
      const r = await post(`proposals/${p.proposal_id || p.id}/${a}`);
      toast(a === "skip" ? "Skipped" : r.note || (r.state === "applied" ? "Applied" : "Done"));
      onDone && onDone();
    } catch (e) { toast(`${a === "skip" ? "Skip" : "Apply"} failed: ${e.message}`); }
  };
  const kind = p.proposal_kind || p.kind;
  return h("li", { class: "strip k-proposal" },
    h("div", { class: "wait" }, h("span", { class: "age" }, age(p.seconds ?? (Date.now() / 1000 - p.created_at))), h("span", { class: "unit" }, " "),
      h("span", { class: "kind" }, kind === "state_doc" ? "state doc" : kind)),
    h("div", { class: "body" },
      h("div", { class: "who" }, h("span", { class: "title" }, p.title || kind), p.work_item && h("span", { class: "chip wi" }, p.work_item)),
      h("div", { class: "ask" }, p.text || p.why || ""),
      h("div", { class: "opts" },
        h("button", { class: "opt go", onclick: () => act("apply") }, kind === "handoff" ? "Confirm done" : "Apply"),
        h("button", { class: "opt", onclick: () => act("skip") }, "Skip"),
        kind === "handoff" && p.session_id && h("a", { class: "btn ghost", href: "#/chat/" + encodeURIComponent(p.session_id) }, "Read the chat first")),
      details));
}

export function renderProposals(el, ov, ui) {
  const list = (ov && ov.proposals) || [];
  if (!list.length) { el.replaceChildren(); return; }
  const open = ui.store.get("propsOpen", false);
  // one decision for the whole list; a handoff stops a running chat, so it is never part of "Apply all"
  const bulk = list.filter((p) => p.proposal_kind !== "handoff");
  const batch = async (e, action) => {
    e.preventDefault(); e.stopPropagation();
    const set = action === "apply" ? bulk : list;
    if (!set.length || !confirm(`${action === "apply" ? "Apply" : "Skip"} ${set.length} proposal${set.length === 1 ? "" : "s"}?`)) return;
    let ok = 0; const bad = [];
    for (const p of set) {
      try { await post(`proposals/${p.proposal_id}/${action}`); ok++; } catch (err) { bad.push(`${p.title}: ${err.message}`); }
    }
    toast(`${action === "apply" ? "Applied" : "Skipped"} ${ok} of ${set.length}${bad.length ? `. Not done: ${bad.join("; ")}` : ""}`);
    ui.refresh();
  };
  const det = h("details", { class: "props", open, ontoggle: () => ui.store.set("propsOpen", det.open) },
    h("summary", { class: "section-head" }, h("h2", {}, "Proposals"),
      h("span", { class: "meta" }, `${list.length}, nothing is written until you Apply; unanswered ones expire after a day`),
      h("span", { class: "grow" }),
      bulk.length > 1 && h("button", { class: "btn", onclick: (e) => batch(e, "apply") }, `Apply all ${bulk.length}`),
      list.length > 1 && h("button", { class: "btn ghost", onclick: (e) => batch(e, "skip") }, `Skip all ${list.length}`)),
    h("ol", { class: "strips" }, list.map((p) => proposalCard(p, ui.refresh))));
  el.replaceChildren(det);
}

// ------------------------------------------------------------------ work item page
export async function renderWorkItem(el, id, ui) {
  const fresh = el.dataset.wi !== id || !el.firstChild;     // a refresh of the same item redraws in place, no skeleton flash
  el.dataset.wi = id;
  if (fresh) el.replaceChildren(h("div", { class: "skeleton" }));
  let w;
  try { w = await get("workitems/" + encodeURIComponent(id)); }
  catch (e) { if (!fresh) return; el.replaceChildren(h("div", { class: "empty" }, h("h3", {}, `Work item ${id}`), h("p", {}, e.message))); return; }
  const props = (w.proposals || []).filter((p) => p.state === "proposed");
  const ta = h("textarea", { class: "reply state-edit", rows: "18", "aria-label": "State of play", spellcheck: "false", readonly: true });
  ta.value = w.state_text || "";
  const edit = h("button", { class: "btn", onclick: () => { ta.readOnly = !ta.readOnly; edit.textContent = ta.readOnly ? "Edit" : "Editing"; save.hidden = ta.readOnly; if (!ta.readOnly) ta.focus(); } }, "Edit");
  const save = h("button", { class: "btn primary", hidden: true, onclick: async () => {
    try { await put(`workitems/${encodeURIComponent(id)}/state`, { text: ta.value }); toast("Saved (previous copy kept in state/.history)"); ta.readOnly = true; save.hidden = true; edit.textContent = "Edit"; }
    catch (e) { toast(`Not saved: ${e.message}`); }
  } }, "Save");
  const regen = h("button", { class: "btn ghost", onclick: async () => {
    try { const r = await post(`workitems/${encodeURIComponent(id)}/regenerate`); toast(r.proposal ? "New proposal below" : r.note || "Nothing changed"); renderWorkItem(el, id, ui); }
    catch (e) { toast(e.message); }
  } }, "Regenerate");
  const tl = (w.timeline || []).map((t) => h("li", { class: "tl-" + t.kind },
    h("span", { class: "tl-at" }, ct(t.at, true)),
    t.kind === "receipt" ? [h("span", { class: "chip " + (t.verified ? "risk-low" : "risk-med") }, t.verified ? "verified" : "not verified"),
      ` ${t.files ?? 0} files `, t.test && h("span", { class: "testline" }, t.test)]
      : t.kind === "decision" ? [h("span", { class: "chip" }, t.state), " ", t.question]
        : [h("span", { class: "chip" }, "final"), " ", h("span", { class: "final inline" }, (t.text || "").slice(-240))]));
  el.replaceChildren(
    h("div", { class: "wi-head" },
      h("div", {}, h("a", { href: "#/", class: "hint" }, "Board"), h("h1", {}, w.title || id),
        h("p", { class: "hint" }, `${id}, suggested seat ${w.home_seat || "none"} (most spare weekly limit), ${(w.sessions || []).length} sessions, state doc ${w.state_path}`)),
      h("button", { class: "btn primary", onclick: () => ui.spawn({ workItem: id, seat: w.home_seat }) }, "Start new session from this")),
    h("div", { class: "wi-grid" },
      h("section", { class: "group wi-state" }, h("h3", {}, "State of play", h("span", { class: "n" }, edit, " ", save, " ", regen)), ta,
        props.length > 0 && h("ol", { class: "strips" }, props.map((p) => proposalCard({ ...p, proposal_kind: p.kind, title: "Proposed update" }, () => renderWorkItem(el, id, ui))))),
      h("div", { class: "wi-side" },
        h("section", { class: "group" }, h("h3", {}, "Sessions", h("span", { class: "n" }, String((w.sessions || []).length))),
          h("ul", { class: "sess" }, (w.sessions || []).map((s) => h("li", {}, h("span", { class: "st " + (limitWord(s, seatsAtLimit(ui.overview && ui.overview(), ui.graphData && ui.graphData())) ? "limited" : s.state), "aria-hidden": "true" }),
            h("span", { class: "nm" }, s.name), h("span", { class: "rt" }, `${s.seat}, ${s.activity ? age(Date.now() / 1000 - s.activity) + " ago" : ""}`))))),
        h("section", { class: "group" }, h("h3", {}, "Timeline"), h("ol", { class: "timeline" }, tl.length ? tl : h("li", { class: "hint" }, "No receipts, decisions or finals yet"))))));
}
