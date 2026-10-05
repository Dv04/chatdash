// Settings (#/settings): every automatic action and its switch, keep-warm, focus mode. Modes are written
// to cp/config.json and read on every use (no restart). Each mode shows what it did (or would have done)
// most recently, from auto_log, so "on" is never a blind switch.
// Sessions (#/sessions): every chat of the last days, with state, cache warmth and keep-warm, linking to history.
import { h, ct, age, toast } from "./lib.js";
import { get, put, post } from "./api.js";
import { resumeSeg } from "./chat.js";

const MODE_HELP = {
  off: "does nothing",
  "dry-run": "logs what it would do, acts on nothing",
  on: "acts",
};

export async function renderSettings(el, ui) {
  el.replaceChildren(h("div", { class: "skeleton" }));
  let s;
  try { s = await get("settings"); } catch (e) { el.replaceChildren(h("div", { class: "page" }, h("div", { class: "empty" }, h("h3", {}, "Settings"), h("p", {}, e.message)))); return; }
  const redraw = () => renderSettings(el, ui);
  const setMode = async (key, mode) => {
    if (mode === "on" && !confirm(`Turn ${key.replace("_", " ")} ON? It will act on live chats from its next tick.`)) return;
    try { await put("settings/modes", { [key]: mode }); toast(`${key}: ${mode}`); redraw(); }
    catch (e) { toast(`Not changed: ${e.message}`); }
  };
  const modes = s.modes.map((m) => h("li", { class: "set-row" },
    h("div", { class: "set-text" },
      h("h3", {}, m.label, m.mode === "on" ? h("span", { class: "chip risk-low" }, "on") : m.mode === "dry-run" ? h("span", { class: "chip ro" }, "dry-run") : h("span", { class: "chip" }, "off")),
      h("p", { class: "hint" }, m.doc),
      m.recent.length ? h("details", { class: "recent" }, h("summary", {}, `Last ${m.recent.length}: ${m.recent[0].decision}, ${age(Date.now() / 1000 - m.recent[0].at)} ago`),
        h("ul", {}, m.recent.map((r) => h("li", {}, h("span", { class: "at" }, ct(r.at, true)), " ", h("b", {}, r.decision), " ", r.reason))))
        : h("p", { class: "hint" }, "Nothing logged yet."),
      m.key === "limit_resume" && h("div", { class: "overrides" },
        h("p", { class: "hint" }, (s.resume_prefs || []).length ? "Per-chat overrides (these win over the switch on the right):" :
          "Per-chat: open a chat (Sessions) and set Limit resume to on to resume only that chat, while this stays dry-run or off."),
        (s.resume_prefs || []).length > 0 && h("ul", { class: "sess" }, s.resume_prefs.map((r) => h("li", {},
          h("span", { class: "st " + (r.pref === "on" ? "working" : "stopped"), "aria-hidden": "true" }),
          h("a", { class: "nm", href: "#/chat/" + encodeURIComponent(r.session_id) }, r.name || r.session_id.slice(0, 8)),
          h("span", { class: "rt" }, r.pref === "on" ? "always resume" : "never resume")))))),
    h("div", { class: "seg", role: "radiogroup", "aria-label": m.label },
      ["off", "dry-run", "on"].map((v) => h("button", { role: "radio", "aria-checked": String(m.mode === v), "aria-pressed": String(m.mode === v),
        title: MODE_HELP[v], onclick: () => m.mode !== v && setMode(m.key, v) }, v)))));

  const kw = s.keepwarm || {};
  const kwAuto = h("li", { class: "set-row" },
    h("div", { class: "set-text" },
      h("h3", {}, "Automatic keep-warm", kw.auto == null ? h("span", { class: "chip risk-med" }, "unknown") : h("span", { class: "chip " + (kw.auto ? "risk-low" : "") }, kw.auto ? "on" : "off")),
      h("p", { class: "hint" }, "Background chats with real activity in the last 12 h get one tiny ping just before their 1 h prompt cache expires. ",
        "Turning it off also ends the chats it switched on; chats you switched on by hand keep their own timer."),
      !kw.available && h("p", { class: "receipt none" }, "Keep-warm runs only in the live server (8787).")),
    kw.available && h("div", { class: "seg", role: "radiogroup", "aria-label": "Automatic keep-warm" },
      [false, true].map((v) => h("button", { role: "radio", "aria-checked": String(kw.auto === v), "aria-pressed": String(kw.auto === v),
        onclick: async () => { if (kw.auto === v) return; try { await put("settings/keepwarm_auto", { on: v }); toast(`Automatic keep-warm ${v ? "on" : "off"}`); redraw(); } catch (e) { toast(e.message); } } },
      v ? "on" : "off"))));
  const kwChats = (kw.chats || []);
  const kwList = h("li", { class: "set-row col" },
    h("h3", {}, "Chats kept warm now", h("span", { class: "n" }, String(kwChats.length))),
    kwChats.length ? h("ul", { class: "sess" }, kwChats.map((r) => h("li", {},
      h("span", { class: "st working", "aria-hidden": "true" }),
      h("a", { class: "nm", href: "#/chat/" + encodeURIComponent(r.key) }, r.name || r.key),
      h("span", { class: "rt" }, `${r.auto ? "auto" : "by hand"}, ${r.pings || 0} pings, ${r.status || ""}`))))
      : h("p", { class: "hint" }, "None. Switch one on from its chat page, or turn automatic keep-warm on."));

  const f = s.focus || {};
  el.replaceChildren(h("div", { class: "page settings" },
    h("a", { href: "#/", class: "hint" }, "Board"), h("h1", {}, "Settings"),
    h("p", { class: "hint" }, "Changes apply on the next tick; nothing restarts. Every switch here is logged."),
    h("section", { class: "group" }, h("h3", {}, "Automatic actions"),
      h("p", { class: "hint pad" }, "off: does nothing. dry-run: logs what it would do. on: acts. Permission prompts are never answered by any of these."),
      h("ul", { class: "set-list" }, modes),
      s.decision_hold_s != null && h("p", { class: "hint pad" }, `Question hold: ${s.decision_hold_s} s (hook timeout 900 s; a 3400 s hold was measured to work).`)),
    h("section", { class: "group" }, h("h3", {}, "Keep-warm"), h("ul", { class: "set-list" }, kwAuto, kwList)),
    h("section", { class: "group" }, h("h3", {}, "Weekly pace"),
      h("ul", { class: "set-list" }, h("li", { class: "set-row" },
        h("div", { class: "set-text" }, h("h3", {}, "Target use per day", h("span", { class: "chip" }, `${(s.pace || {}).pct_per_day}%`)),
          h("p", { class: "hint" }, "Share of the 7-day limit to use each day. 14.3% spreads 100% evenly over the week; 15% lands near 105%. ",
            "Capacity marks each seat's even-pace point and lists the seats behind it as use first.")),
        paceEditor(s, redraw, ui)))),
    h("section", { class: "group" }, h("h3", {}, "Look"),
      h("ul", { class: "set-list" }, h("li", { class: "set-row" },
        h("div", { class: "set-text" }, h("h3", {}, "Look", h("span", { class: "chip" }, ui.look())),
          h("p", { class: "hint" }, "Stored in this browser only. Nebula is the new look for every view (Sky, board, graph, chats, settings); current is the default. ",
            "?look=nebula in the address switches it for one visit.")),
        h("div", { class: "seg", role: "radiogroup", "aria-label": "Board look" },
          ["current", "nebula"].map((v) => h("button", { role: "radio", "aria-checked": String(ui.look() === v), "aria-pressed": String(ui.look() === v),
            onclick: () => { if (ui.look() !== v) { ui.setLook(v); redraw(); } } }, v)))))),
    h("section", { class: "group" }, h("h3", {}, "Notifications"),
      h("ul", { class: "set-list" }, h("li", { class: "set-row" },
        h("div", { class: "set-text" }, h("h3", {}, "Focus mode", h("span", { class: "chip " + (f.on ? "risk-low" : "") }, f.on ? "on" : "off")),
          h("p", { class: "hint" }, `macOS notifications only for decisions older than ${f.min_age_min} min; the rest arrive at ${(f.windows || []).join(" and ")} (local time).`)),
        h("button", { class: "btn", onclick: () => ui.toggleFocusPanel() }, "Edit")))),
    h("section", { class: "group" }, h("h3", {}, "About"),
      h("p", { class: "hint pad" }, "chatdash, built by Dev Sanghvi at ",
        h("a", { href: "https://dhi-tech.com", target: "_blank", rel: "noopener" }, "DHI"), "."))));
}

function paceEditor(s, redraw, ui) {
  const inp = h("input", { type: "number", class: "field num", min: "5", max: "30", step: "0.1", value: String((s.pace || {}).pct_per_day ?? 14.3), "aria-label": "Percent per day" });
  return h("div", { class: "thresholds" }, h("label", {}, inp, " % a day"),
    h("button", { class: "btn", onclick: async () => {
      try { const r = await put("settings/pace", { pct_per_day: Number(inp.value) }); ui.pace = r.pace; ui.rerender(); toast("Pace saved"); redraw(); }
      catch (e) { toast(`Not saved: ${e.message}`); } } }, "Save"));
}

export async function renderSessions(el, ui) {
  if (!el.firstChild) el.replaceChildren(h("div", { class: "skeleton" }));
  let d;
  try { d = await get("sessions"); } catch (e) { el.replaceChildren(h("div", { class: "page" }, h("div", { class: "empty" }, h("h3", {}, "Sessions"), h("p", {}, e.message)))); return; }
  const q = (ui.sessQ || "").toLowerCase();
  const all = d.sessions;
  const show = all.filter((s) => (ui.sessAll || s.state !== "stopped") && (!q || [s.name, s.seat, s.work_item].some((v) => v && v.toLowerCase().includes(q))));
  const find = h("input", { type: "search", class: "field", placeholder: "Filter by name, seat, work item", value: ui.sessQ || "", "aria-label": "Filter sessions",
    oninput: (e) => { ui.sessQ = e.target.value; renderSessions(el, ui); } });
  const rows = show.map((s) => h("tr", {},
    h("td", {}, h("span", { class: "st " + (s.limited ? "limited" : s.state), "aria-hidden": "true" }), " ", s.live ? s.state : "not running"),
    h("td", { class: "nm" }, h("a", { href: "#/chat/" + encodeURIComponent(s.session_id) }, s.name)),
    h("td", {}, s.seat, s.excluded && h("span", { class: "hint" }, " (ro)")),
    h("td", {}, s.work_item || ""),
    h("td", { class: "num" }, s.activity ? age(Date.now() / 1000 - s.activity) : "?"),
    h("td", {}, s.cache_age_min == null ? "" : `${s.warmth || ""} ${Math.round(s.cache_age_min)}m`),
    h("td", {}, s.kw && s.kw.on ? `on, ${s.kw.pings} pings` : ""),
    h("td", {}, !s.resume.available ? h("span", { class: "hint" }, "n/a") :
      h("select", { class: "field", "aria-label": `Limit resume for ${s.name}`, onchange: async (e) => {
        try { await post(`sessions/${encodeURIComponent(s.session_id)}/resume_pref`, { pref: e.target.value }); toast("Saved"); renderSessions(el, ui); }
        catch (err) { toast(`Not changed: ${err.message}`); } } },
        [["default", `default (${s.resume.global})`], ["on", "on"], ["off", "off"]].map(([v, l]) =>
          h("option", { value: v, selected: (s.resume.pref || "default") === v }, l))))));
  const focused = document.activeElement && document.activeElement.getAttribute("aria-label") === "Filter sessions";
  el.replaceChildren(h("div", { class: "page sessions" },
    h("a", { href: "#/", class: "hint" }, "Board"), h("h1", {}, "Sessions"),
    h("div", { class: "chat-tools" }, find,
      h("button", { class: "btn ghost", "aria-pressed": String(!!ui.sessAll), onclick: () => { ui.sessAll = !ui.sessAll; renderSessions(el, ui); } },
        ui.sessAll ? "Hide stopped" : `Show stopped (${all.filter((s) => s.state === "stopped").length})`)),
    h("div", { class: "table-wrap" }, h("table", { class: "sess-table" },
      h("thead", {}, h("tr", {}, ["State", "Chat", "Seat", "Work item", "Last activity", "Cache", "Keep-warm", "Limit resume"].map((t) => h("th", {}, t)))),
      h("tbody", {}, rows.length ? rows : h("tr", {}, h("td", { colspan: "8", class: "hint" }, "No sessions match")))))));
  if (focused) { const i = el.querySelector('input[aria-label="Filter sessions"]'); i.focus(); i.setSelectionRange(i.value.length, i.value.length); }
}

export { post };
