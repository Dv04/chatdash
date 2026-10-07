// Codex and Cursor chats next to the Claude board. Plain script, no modules: it is one self-contained page.
(function () {
  "use strict";
  var TOKEN = window.CP_TOKEN && window.CP_TOKEN !== "__TOKEN__" ? window.CP_TOKEN : "";
  var S = { providers: [], tab: "all", cur: null, hours: 168, sending: false };
  var $ = function (id) { return document.getElementById(id); };
  try { S.tab = localStorage.getItem("orbit.ai.tab") || "all"; S.hours = +(localStorage.getItem("orbit.ai.hours") || 168); } catch (e) { /* storage blocked */ }
  $("hours").value = String(S.hours);

  function api(method, path, body) {
    return fetch(path, { method: method, cache: "no-store", headers: Object.assign({ "X-Token": TOKEN }, body ? { "Content-Type": "application/json" } : {}),
      body: body ? JSON.stringify(body) : undefined }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) { if (!r.ok) throw new Error(j.error || ("HTTP " + r.status)); return j; });
    });
  }
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
  function ago(t) {
    if (!t) return ""; var s = Math.max(0, Date.now() / 1000 - t);
    if (s < 90) return "just now"; if (s < 5400) return Math.round(s / 60) + " min ago";
    if (s < 129600) return Math.round(s / 3600) + " h ago"; return Math.round(s / 86400) + " days ago";
  }
  function allChats() {
    var out = [];
    S.providers.forEach(function (p) { if (S.tab === "all" || S.tab === p.id) p.chats.forEach(function (c) { out.push(c); }); });
    return out.sort(function (a, b) { return b.updated_at - a.updated_at; });
  }
  function renderTabs() {
    var box = $("tabs"); box.textContent = "";
    [{ id: "all", label: "All" }].concat(S.providers.map(function (p) { return { id: p.id, label: p.label + " (" + p.chats.length + ")" }; })).forEach(function (t) {
      var b = el("button", "tab", t.label); b.type = "button"; b.setAttribute("aria-pressed", String(S.tab === t.id));
      b.onclick = function () { S.tab = t.id; try { localStorage.setItem("orbit.ai.tab", t.id); } catch (e) { /* ignore */ } renderTabs(); renderList(); };
      box.appendChild(b);
    });
  }
  function renderList() {
    var box = $("list"); box.textContent = "";
    S.providers.forEach(function (p) {
      if (p.error) box.appendChild(el("div", "err", p.label + ": " + p.error));
      else if (!p.available && (S.tab === "all" || S.tab === p.id)) box.appendChild(el("div", "empty", p.label + " not found" + (p.detail ? ": " + p.detail : "")));
    });
    var cs = allChats();
    if (!cs.length && !box.childNodes.length) box.appendChild(el("div", "empty", "No chats in this window."));
    cs.forEach(function (c) {
      var b = el("button", "row"); b.type = "button";
      if (S.cur && S.cur.key === c.key) b.setAttribute("aria-current", "true");
      var t = el("div", "t"); t.appendChild(el("span", "n", c.name));
      t.appendChild(el("span", "pill " + c.state, c.state)); b.appendChild(t);
      var m = el("div", "m"); m.appendChild(el("span", null, c.provider)); m.appendChild(el("span", null, ago(c.updated_at)));
      if (c.cwd) m.appendChild(el("span", null, c.cwd.split(/[\\/]/).slice(-2).join("/"))); b.appendChild(m);
      if (c.error) b.appendChild(el("div", "f err", c.error)); else if (c.final) b.appendChild(el("div", "f", c.final));
      b.onclick = function () { open(c); };
      box.appendChild(b);
    });
  }
  function open(c) {
    S.cur = c; renderList(); location.hash = "#" + c.key;
    api("GET", "/api/ai/chat?provider=" + encodeURIComponent(c.provider) + "&id=" + encodeURIComponent(c.id)).then(function (j) { renderChat(j); })
      .catch(function (e) { var v = $("view"); v.textContent = ""; v.appendChild(el("div", "err", "Could not load this chat: " + e.message)); });
  }
  function renderChat(j) {
    var c = j.chat, v = $("view"), keep = (v.querySelector("textarea") || {}).value || ""; v.textContent = "";
    v.appendChild(el("h2", null, c.name));
    v.appendChild(el("div", "sub", [c.provider, c.state, c.model, ago(c.updated_at), c.cwd].filter(Boolean).join("  |  ")));
    if (c.error) v.appendChild(el("div", "err", c.error));
    if (!j.turns.length) v.appendChild(el("div", "empty", c.history === "legacy" ? "This older Codex thread keeps its messages in an older format; only the last answer is shown." : "No messages found."));
    if (!j.turns.length && c.final) { var t0 = el("div", "turn"); t0.appendChild(el("div", "who", "assistant")); t0.appendChild(document.createTextNode(c.final)); v.appendChild(t0); }
    j.turns.forEach(function (t) {
      var d = el("div", "turn " + t.role + (t.phase === "commentary" ? " commentary" : "")); d.appendChild(el("div", "who", t.role + (t.at ? "  " + ago(t.at) : "")));
      d.appendChild(document.createTextNode(t.text)); v.appendChild(d);
    });
    var f = el("form"); var ta = el("textarea"); ta.placeholder = "Reply to " + c.provider + " (Ctrl/Cmd+Enter to send)"; ta.value = keep; ta.setAttribute("aria-label", "Reply");
    var bar = el("div", "bar"); var go = el("button", "go", "Send"); go.type = "submit"; var msg = el("span"); msg.id = "msg";
    bar.appendChild(go); bar.appendChild(msg); f.appendChild(ta); f.appendChild(bar); v.appendChild(f);
    ta.addEventListener("keydown", function (e) { if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); f.requestSubmit(); } });
    f.onsubmit = function (e) {
      e.preventDefault(); var text = ta.value.trim(); if (!text || S.sending) return;
      S.sending = true; go.disabled = true; msg.textContent = "Sending (can take a few seconds)...";
      api("POST", "/api/ai/reply", { provider: c.provider, id: c.id, text: text }).then(function (r) {
        if (r.ok) { ta.value = ""; msg.textContent = r.started ? "Started. The turn is running; this page updates when it finishes." : "Done."; }
        else msg.textContent = (r.error || "failed");
        setTimeout(refresh, 1500);
      }).catch(function (er) { msg.textContent = er.message; }).then(function () { S.sending = false; go.disabled = false; });
    };
  }
  function refresh() {
    return api("GET", "/api/ai?hours=" + S.hours).then(function (j) {
      S.providers = j.providers; renderTabs(); renderList();
      if (!S.cur) { var k = decodeURIComponent(location.hash.slice(1)); var found = allChats().filter(function (c) { return c.key === k; })[0]; if (found) open(found); }
      else {
        var fresh = allChats().filter(function (c) { return c.key === S.cur.key; })[0];
        var ta = document.querySelector("#view textarea");
        if (fresh && fresh.updated_at !== S.cur.updated_at && !(ta && ta.value) && !S.sending) open(fresh);   // new output, and nothing being typed
        else if (fresh) S.cur = fresh;
      }
    }).catch(function (e) { $("list").textContent = ""; $("list").appendChild(el("div", "err", "Could not load: " + e.message)); });
  }
  $("hours").onchange = function () { S.hours = +this.value; try { localStorage.setItem("orbit.ai.hours", String(S.hours)); } catch (e) { /* ignore */ } refresh(); };
  refresh(); setInterval(function () { if (!document.hidden) refresh(); }, 6000);
})();
