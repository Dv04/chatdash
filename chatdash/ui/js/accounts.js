// Accounts (Settings > Accounts, and the first-run screen): connect a Claude account, sign in, disconnect, delete.
// Signing in runs the official `claude auth login` on the computer running chatdash: Claude Code opens that
// computer's browser at claude.com and finishes by itself. From another device, open the fallback link, approve,
// and paste the code it shows here; it is typed into Claude Code's own prompt and never stored.
import { h, toast } from "./lib.js";
import { get, post } from "./api.js";

export async function renderAccounts(el, ui, { firstRun = false } = {}) {
  clearTimeout(el._acctPoll);                // per panel: Settings and the first-run screen can both be open
  let rows;
  try { rows = (await get("accounts")).accounts; ui && ui.accountsSeen && ui.accountsSeen(rows); }
  catch (e) { el.replaceChildren(h("p", { class: "hint" }, `Accounts unavailable: ${e.message}`)); return; }
  const redraw = () => renderAccounts(el, ui, { firstRun });
  const act = async (path, body, ok) => {
    try { const r = await post(path, body || {}); toast(r.note || ok); redraw(); ui && ui.refresh && ui.refresh(); return r; }
    catch (e) { toast(e.message); return null; }
  };

  const add = (() => {
    const name = h("input", { type: "text", class: "field", placeholder: "account name, e.g. work", "aria-label": "New account name",
      autocapitalize: "off", autocomplete: "off", spellcheck: "false", maxlength: "24",
      oninput: (e) => { e.target.value = e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, ""); } });
    const console_ = h("input", { type: "checkbox", id: "acct-console" });
    const go = async () => {
      const n = name.value.trim();
      if (!n) { name.focus(); return; }
      try { await post("accounts", { name: n, console: console_.checked }); redraw(); }
      catch (e) { toast(e.message); }
    };
    name.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
    return h("div", { class: "acct-add" },
      h("div", { class: "replyrow" }, name, h("button", { class: "btn primary", onclick: go }, "Connect account")),
      h("label", { class: "hint" }, console_, " Anthropic Console account (API billing) instead of a Claude subscription"));
  })();

  const loginBox = (r) => {
    const lg = r.login;
    if (!lg || !["starting", "waiting"].includes(lg.state)) {
      if (lg && lg.state === "failed") return h("p", { class: "acct-err" }, `Sign-in failed: ${lg.error || "unknown"}`);
      if (lg && lg.state === "cancelled") return h("p", { class: "hint" }, "Sign-in cancelled.");
      return null;
    }
    const code = h("input", { type: "text", class: "field", placeholder: "Paste the code from claude.com", "aria-label": "Sign-in code",
      autocapitalize: "off", autocomplete: "off", spellcheck: "false" });
    const send = async () => {
      if (!code.value.trim()) return;
      try { await post(`accounts/${encodeURIComponent(r.name)}/code`, { code: code.value.trim() }); code.value = ""; toast("Code sent to Claude Code"); }
      catch (e) { toast(e.message); }
    };
    code.addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });
    return h("div", { class: "acct-login", role: "status" },
      h("p", {}, h("b", {}, "Signing in. "), "A claude.com sign-in page opened in the browser on the computer running chatdash. Approve there and this finishes by itself."),
      lg.link && h("p", { class: "hint" }, "On another device: ",
        h("a", { href: lg.link, target: "_blank", rel: "noopener noreferrer" }, "open the sign-in link"),
        ", approve, then paste the code it shows:"),
      lg.link && h("div", { class: "replyrow" }, code, h("button", { class: "btn", onclick: send }, "Send code")),
      lg.notice && h("p", { class: "acct-err", role: "alert" }, "Claude Code says: ", lg.notice),
      h("div", { class: "acct-ctl" },
        lg.notice && h("button", { class: "btn", onclick: () => act("accounts", { name: r.name, restart: true }, "New sign-in link") }, "Start over"),
        h("button", { class: "btn ghost", onclick: () => act(`accounts/${encodeURIComponent(r.name)}/cancel`, {}, "Cancelled") }, "Cancel")));
  };

  const row = (r) => {
    const status = r.signed_in === true ? h("span", { class: "chip risk-low" }, "signed in")
      : r.signed_in === false ? h("span", { class: "chip risk-med" }, "not signed in")
      : h("span", { class: "chip ro", title: r.error || "" }, "status unknown");
    const who = [r.email, r.plan && `${r.plan} plan`, r.method && r.method !== "claude.ai" && r.method].filter(Boolean).join(", ");
    const del = async () => {
      const typed = prompt(r.name === "main"
        ? `Sign out "main" (your default ~/.claude)? It is signed out and hidden, not deleted. Type main to confirm.`
        : `Delete account "${r.name}"? It is signed out and its folder ${r.dir} moves to the Trash. Type ${r.name} to confirm.`);
      if (typed == null) return;
      await act(`accounts/${encodeURIComponent(r.name)}/delete`, { confirm: typed.trim() }, r.name === "main" ? "Signed out" : "Deleted (moved to the Trash)");
    };
    return h("li", { class: "set-row acct" + (r.hidden ? " hidden-acct" : "") },
      h("div", { class: "set-text" },
        h("h3", {}, r.label, " ", status, r.hidden && h("span", { class: "chip" }, "disconnected"), r.read_only && h("span", { class: "chip ro" }, "read-only"),
          r.running ? h("span", { class: "chip" }, `${r.running} Claude process${r.running === 1 ? "" : "es"} open`) : ""),
        h("p", { class: "hint" }, who || (r.signed_in ? "" : "Connect it to see its chats and limits here."), " ", h("code", {}, r.dir)),
        loginBox(r)),
      h("div", { class: "set-ctl acct-ctl" },
        r.signed_in !== true && !(r.login && ["starting", "waiting"].includes(r.login.state)) &&
          h("button", { class: "btn primary", onclick: () => act("accounts", { name: r.name }, "Signing in") }, "Sign in"),
        r.hidden ? h("button", { class: "btn", onclick: () => act(`accounts/${encodeURIComponent(r.name)}/reconnect`, {}, "Reconnected") }, "Reconnect")
          : h("button", { class: "btn ghost", title: "Hide this account from the board; its sign-in and chats are untouched",
            onclick: () => act(`accounts/${encodeURIComponent(r.name)}/disconnect`, {}, "Disconnected: hidden from the board") }, "Disconnect"),
        h("button", { class: "btn ghost danger", disabled: r.running > 0, title: r.running ? "Close or stop its open Claude sessions first" : "", onclick: del },
          r.name === "main" ? "Sign out" : "Delete")));
  };

  el.replaceChildren(h("div", { class: "accounts" },
    firstRun && h("div", { class: "empty" }, h("h3", {}, "Connect your first Claude account"),
      h("p", {}, "chatdash shows the Claude Code chats on this computer. Each account is a Claude Code folder (~/.claude or ~/.claude-<name>). Signing in uses Claude Code's own login; chatdash never sees your password.")),
    rows.length ? h("ul", { class: "set-list" }, rows.map(row)) : !firstRun && h("p", { class: "hint" }, "No accounts yet."),
    h("h4", {}, "Add an account"), add,
    h("p", { class: "hint" }, "Each account gets its own folder, ~/.claude-<name>. Run chats on it with CLAUDE_CONFIG_DIR=~/.claude-<name> claude, or start them from the board.")));

  if (rows.some((r) => r.login && ["starting", "waiting"].includes(r.login.state)))
    el._acctPoll = setTimeout(() => { if (el.isConnected && !el.contains(document.activeElement)) redraw(); else if (el.isConnected) el._acctPoll = setTimeout(redraw, 1500); }, 1500);
}
