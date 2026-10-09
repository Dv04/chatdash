// Spawn dialog: start a named background session on a work item. The user chooses the seat every time
// : the suggested seat is preselected and every seat shows its headroom.
import { h, toast, ct } from "./lib.js";
import { get, post } from "./api.js";
import { micFor } from "./dictate.js";
import { ATTACH_MAX, ATTACH_ACCEPT, attachNote, uploadFile, fmtSize } from "./chat.js";

const BIG_WORDS = /\b(build|design|migrate|refactor|plan|research|new product)\b/i;
export function interviewRule(brief) {
  const words = (brief || "").trim().split(/\s+/).filter(Boolean).length;
  if (words > 60) return `brief is ${words} words (over 60)`;
  const m = (brief || "").match(BIG_WORDS);
  return m ? `brief mentions "${m[0].toLowerCase()}"` : "";
}

function headroom(s) {
  const f = s.five_hour.pct, w = s.seven_day.pct;
  const part = (p, l) => (p == null ? `${l} ?` : `${l} ${Math.round(p)}%`);
  return `${part(f, "5h")}, ${part(w, "7d")}${s.resume_at ? `, resumes ${ct(s.resume_at)}` : ""}`;
}

let dlg;
export async function spawnDialog({ workItem, seat, parent, seats, onDone, brief: givenBrief, cwd, title }) {
  // The page's overview feed refreshes every 5 s: a tap right after load finds no seats yet, so ask for them directly.
  if (!seats || !seats.length) { try { seats = (await get("capacity")).seats || []; } catch { seats = []; } }
  let wi = null, brief = "";
  try {
    wi = workItem ? await get("workitems/" + encodeURIComponent(workItem)) : null;
    const b = workItem ? await get("workitems/" + encodeURIComponent(workItem) + "/brief") : null;
    brief = (b && b.brief) || "";
  } catch { /* brief endpoint may not exist yet: start empty */ }
  if (givenBrief) brief = givenBrief + (brief ? "\n\n" + brief : "");
  let wiList = [];
  try { wiList = (await get("workitems")).work_items || []; } catch { /* no work items: the picker offers free chat only */ }
  const home = (wi && wi.home_seat) || null;
  const pick = seat || home;
  dlg = dlg || document.body.appendChild(h("dialog", { class: "spawn", "aria-label": "Start a session" }));
  const sel = h("select", { id: "sp-seat", "aria-label": "Seat" }, seats.filter((s) => !s.excluded).map((s) =>
    h("option", { value: s.seat, selected: s.seat === pick }, `${s.seat}${s.seat === home ? " (most spare)" : ""}: ${s.state}, ${headroom(s)}`)));
  const ta = h("textarea", { id: "sp-brief", rows: "8", "aria-label": "Brief" });
  ta.value = brief;
  const why = h("span", { class: "hint" });
  // The rule ticks the box until the user changes it by hand; from then on typing never flips it back (only the hint follows the brief).
  let ivManual = false;
  const iv = h("input", { type: "checkbox", id: "sp-iv", onchange: () => { ivManual = true; sync(); } });
  const sync = () => {
    const r = interviewRule(ta.value);
    if (!ivManual) iv.checked = !!r;
    why.textContent = ivManual ? (iv.checked ? "on (your choice)" : "off (your choice)") + (r ? `; the rule would say on: the ${r}` : "")
      : r ? `on because the ${r}` : "off: short, routine brief";
  };
  ta.addEventListener("input", sync);
  sync();
  // Where the chat starts. Claude Code will not start a background chat in the home folder, so the default is the newest trusted
  // project folder (or `default_cwd` from the config); the list holds recent chats' folders and trusted projects.
  let fd = { default: "", folders: [] };
  try { fd = await get("folders?seat=" + encodeURIComponent(pick || (seats[0] && seats[0].seat) || "")); } catch { /* no suggestions: the field stays free text */ }
  const folderIn = h("input", { type: "text", class: "free", id: "sp-folder", list: "sp-folders", value: cwd || fd.default || "", spellcheck: "false",
    "aria-label": "Folder", placeholder: "Folder the chat starts in" });
  const folderList = h("datalist", { id: "sp-folders" }, (fd.folders || []).map((f) => h("option", { value: f.path }, f.trusted ? "trusted" : "")));
  // Attachments: uploaded at once (same endpoint and cap as the chat reply box); their paths go at the end of the brief.
  const files = [];
  const strip = h("div", { class: "attach-strip", hidden: true, "aria-live": "polite" });
  const paintFiles = () => {
    strip.hidden = !files.length;
    strip.replaceChildren(...files.map((f) => h("span", { class: "attach-chip" + (f.err ? " err" : "") },
      h("span", { class: "nm", title: f.path || f.err || "" }, f.name),
      h("span", { class: "hint" }, f.err || (f.path ? fmtSize(f.size) : "uploading")),
      h("button", { type: "button", class: "x", "aria-label": `Remove ${f.name}`, onclick: () => { const i = files.indexOf(f); if (i >= 0) files.splice(i, 1); paintFiles(); } }, "\u00d7"))));
  };
  const addFiles = async (list) => {
    for (const file of [...list]) {
      const f = { name: file.name || `pasted-${Date.now()}.png`, size: file.size };
      if (file.size > ATTACH_MAX) { toast(`${f.name} is over ${ATTACH_MAX / 1048576} MB`); continue; }
      files.push(f); paintFiles();
      try { Object.assign(f, await uploadFile(file, f.name)); } catch (e) { f.err = e.message; toast(`Not attached: ${e.message}`); }
      paintFiles();
    }
  };
  const filePick = h("input", { type: "file", multiple: true, hidden: true, accept: ATTACH_ACCEPT, onchange: (e) => { addFiles(e.target.files); e.target.value = ""; } });
  ta.addEventListener("paste", (e) => { const fs = e.clipboardData && e.clipboardData.files; if (fs && fs.length) { e.preventDefault(); addFiles(fs); } });
  const warn = h("p", { class: "hint", id: "sp-warn" });
  const seatWarn = () => {
    const s = seats.find((x) => x.seat === sel.value);
    warn.textContent = s && (s.state === "blocked" || s.state === "near")
      ? `${s.seat} is ${s.state}: the session is queued and starts when the seat has headroom (it never moves to another seat by itself).` : "";
  };
  sel.addEventListener("change", seatWarn);
  seatWarn();
  // Work item picker: choosing one loads its brief (state of play) and suggests its seat; a brief the user edited is never overwritten.
  let curWi = workItem || "", autoBrief = brief;
  const wsel = h("select", { id: "sp-wi", "aria-label": "Work item" }, h("option", { value: "" }, "None (free chat)"),
    wiList.map((w) => h("option", { value: w.id, selected: w.id === curWi }, w.title || w.id)));
  wsel.addEventListener("change", async () => {
    curWi = wsel.value;
    if (!curWi) { if (ta.value === autoBrief) { ta.value = ""; autoBrief = ""; } sync(); return; }
    try {
      const b = await get("workitems/" + encodeURIComponent(curWi) + "/brief");
      if (!ta.value.trim() || ta.value === autoBrief) { ta.value = (b && b.brief) || ""; autoBrief = ta.value; }
      if (b && b.home_seat && [...sel.options].some((o) => o.value === b.home_seat)) { sel.value = b.home_seat; seatWarn(); }
      sync();
    } catch (err) { toast(`Could not load ${curWi}: ${err.message}`); }
  });
  const start = h("button", { class: "btn primary", onclick: async (e) => {
    e.preventDefault();
    if (files.some((f) => !f.path && !f.err)) { toast("Still uploading"); return; }
    const done = files.filter((f) => f.path);
    let text = ta.value.trim();
    if (done.length) text += (text ? "\n\n" : "") + attachNote(done);
    start.disabled = true;
    try {
      const res = await post("sessions", { work_item: curWi || undefined, seat: sel.value, brief: text, interview: iv.checked, parent, cwd: folderIn.value.trim() || undefined });
      toast(res.queued ? `Queued on ${sel.value}: starts when the seat has headroom` : `Started on ${sel.value} (job ${res.job_id || "?"})`);
      dlg.close();
      onDone && onDone(res);
    } catch (err) {
      toast(`Not started: ${err.message}${err.data && err.data.milestone ? ` (arrives in ${err.data.milestone})` : ""}`);
    } finally { start.disabled = false; }
  } }, "Start session");
  dlg.replaceChildren(h("form", { method: "dialog", class: "spawn-form" },
    h("h3", {}, title || (workItem ? `New session: ${(wi && wi.title) || workItem}` : "New session")),
    parent && h("p", { class: "hint" }, `Child of ${parent}`),
    h("label", { for: "sp-folder" }, "Folder"), folderIn, folderList,
    h("p", { class: "hint" }, "Claude Code starts a background chat only in a folder it trusts: a project you have opened in `claude` before, not your home folder."),
    h("label", { for: "sp-wi" }, "Work item"), wsel,
    h("label", { for: "sp-seat" }, "Seat"), sel, warn,
    h("label", { for: "sp-brief" }, "Brief (from the work item's state of play; edit freely)"), h("div", { class: "replyrow" }, ta, micFor(ta)),
    strip, h("div", { class: "row" }, h("button", { type: "button", class: "btn ghost attach-btn", title: "Attach images, PDFs or documents (or paste them into the brief, or drop them here)",
      onclick: () => filePick.click() }, "Attach files"), filePick),
    h("label", { for: "sp-iv", class: "row", title: "The new chat first asks you questions one decision at a time (the AskUserQuestion dialog), then writes the agreed spec into the work item's state doc and starts the work. Auto-on for long or build/plan/research briefs." },
      iv, " Interview me first, then write the spec"), why,
    h("div", { class: "row" }, start, h("button", { class: "btn ghost", value: "cancel" }, "Cancel"))));
  dlg.ondragover = (e) => { if (e.dataTransfer && [...(e.dataTransfer.types || [])].includes("Files")) e.preventDefault(); };
  dlg.ondrop = (e) => { if (e.dataTransfer && e.dataTransfer.files.length) { e.preventDefault(); addFiles(e.dataTransfer.files); } };
  dlg.showModal();
  ta.focus();
}
