// Docs reader: the .md files of a chat (made in it, or named in it), read right beside the chat instead of being told
// "the full write-up is in docs/X.md". A panel docked on the right: on a wide screen the page moves over to make
// room, so the chat stays fully readable; drag its left edge to resize; on a phone it is a full-screen sheet.
// Rendering is real GitHub-flavoured markdown (marked), sanitized (DOMPurify), with highlighted code, a contents outline,
// working links between documents, and live re-reading when the chat edits the open file.
import { h, age, toast } from "./lib.js";
import { get } from "./api.js";
import { marked } from "./vendor/marked.esm.js";
import DOMPurify from "./vendor/purify.es.js";
import hljs from "./vendor/hljs/core.js";
import bash from "./vendor/hljs/bash.js";
import shell from "./vendor/hljs/shell.js";
import python from "./vendor/hljs/python.js";
import javascript from "./vendor/hljs/javascript.js";
import typescript from "./vendor/hljs/typescript.js";
import json from "./vendor/hljs/json.js";
import yaml from "./vendor/hljs/yaml.js";
import diff from "./vendor/hljs/diff.js";
import sql from "./vendor/hljs/sql.js";
import css from "./vendor/hljs/css.js";
import xml from "./vendor/hljs/xml.js";
import markdown from "./vendor/hljs/markdown.js";
import ini from "./vendor/hljs/ini.js";
import plaintext from "./vendor/hljs/plaintext.js";
import go from "./vendor/hljs/go.js";
import rust from "./vendor/hljs/rust.js";

for (const [n, f, al] of [["bash", bash, ["sh", "zsh"]], ["shell", shell, ["console"]], ["python", python, ["py"]], ["javascript", javascript, ["js", "mjs"]],
  ["typescript", typescript, ["ts", "tsx"]], ["json", json, ["jsonl"]], ["yaml", yaml, ["yml"]], ["diff", diff, ["patch"]], ["sql", sql], ["css", css],
  ["xml", xml, ["html", "svg"]], ["markdown", markdown, ["md"]], ["ini", ini, ["toml"]], ["plaintext", plaintext, ["text", "txt", "toon"]], ["go", go], ["rust", rust, ["rs"]]]) {
  hljs.registerLanguage(n, f);
  if (al) hljs.registerAliases(al, { languageName: n });
}
marked.setOptions({ gfm: true, breaks: false });

const W_MIN = 560, PAGE_MIN = 680, SIZES = [14, 15, 16, 17, 18, 20];
const slug = (s) => s.toLowerCase().trim().replace(/[^\w\s-]/g, "").replace(/\s+/g, "-");

export class DocsDrawer {
  constructor(ui) {
    this.ui = ui; this.sid = null; this.docs = []; this.cur = null; this.raw = false; this.ret = null; this.filter = "";
    this.w = Number(ui.store.get("docsW", 0)) || 0;
    this.size = Number(ui.store.get("docsSize", 16)) || 16;
    this.toc = ui.store.get("docsToc", true) !== false;
    this.el = h("aside", { class: "doc-drawer", hidden: true, role: "complementary", "aria-label": "Documents of this chat" });
    document.body.append(this.el);
    // Esc closes the switcher first, then the panel, wherever the focus is (capture: before the page's own Esc), unless a dialog is up.
    document.addEventListener("keydown", (e) => {
      if (!this.isOpen || e.defaultPrevented || document.querySelector("dialog[open], .keys-sheet")) return;
      const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName) || e.target.isContentEditable;
      if (e.key === "Escape") {
        e.preventDefault(); e.stopPropagation();
        if (this.pick && !this.pick.hidden) { this.pick.hidden = true; this.switchBtn && this.switchBtn.focus(); } else this.close();
      } else if (!typing && this.el.contains(e.target) && (e.key === "[" || e.key === "]")) { e.preventDefault(); this.step(e.key === "]" ? 1 : -1); }
    }, true);
    window.addEventListener("resize", () => { if (this.isOpen) this.dock(); }, { passive: true });
  }
  get isOpen() { return !this.el.hidden; }

  // The chat's list; called on every chat open and refresh (cheap: the server reads only new transcript bytes).
  async load(sid) {
    if (sid !== this.sid) { this.sid = sid; this.docs = []; if (this.isOpen) this.cur = null; }
    let same = false;
    try {
      const d = await get(`sessions/${encodeURIComponent(sid)}/docs`);
      if (sid !== this.sid) return this.docs;
      same = JSON.stringify(d.docs || []) === JSON.stringify(this.docs); this.docs = d.docs || [];
    } catch { return this.docs; }
    if (this.isOpen && !(same && this.cur)) {      // repaint only on a change: it would drop the scroll and focus
      const c = this.cur && this.docs.find((x) => x.path === this.cur.path);
      if (c && c.mtime && this.cur.mtime && c.mtime !== this.cur.mtime) this.show(c.path, { keep: true });   // the chat edited the open document
      else if (!this.cur && this.docs.length) this.show(this.docs[0].path);
      else this.paintHead();
    }
    return this.docs;
  }

  open(path) {
    if (!this.isOpen) {
      this.ret = document.activeElement; this.el.hidden = false; this.dock();
      requestAnimationFrame(() => this.el.classList.add("in"));
    }
    if (path) this.show(path);
    else if (!this.cur && this.docs.length) this.show(this.docs[0].path);
    else this.paint();
  }

  close() {
    if (!this.isOpen) return;
    this.el.classList.remove("in"); this.el.hidden = true; this.undock();
    if (this.ret && document.contains(this.ret)) this.ret.focus({ preventScroll: true });
  }

  // Wide screen: the page gets a right margin the width of the panel, so nothing is covered. Narrow: the panel overlays.
  // Docked, the page keeps at least PAGE_MIN px (a panel dragged too wide squeezed the rail counts to one word per line).
  // Docked, the page compacts (icon-only rail tools, one-line counts, list from 150 px, chat from ~400 px: css
  // "doc-docked"), so 680 px is enough and the panel can be wide. No room for W_MIN beside it: the panel overlays.
  width() { return Math.round(Math.min(Math.max(this.w || Math.min(900, innerWidth * 0.5), W_MIN), innerWidth - 48)); }
  dock() {
    const room = innerWidth - PAGE_MIN, docked = room >= W_MIN && innerWidth > 760;
    const w = docked ? Math.min(this.width(), room) : Math.min(this.width(), Math.max(W_MIN, innerWidth - 320), innerWidth);   // overlay: some page stays visible
    document.documentElement.style.setProperty("--doc-w", `${Math.round(w)}px`);
    const was = this.el.classList.contains("narrow");
    this.el.classList.toggle("narrow", w < 760);          // too narrow for the outline beside the text: Contents opens it over the text
    if (was !== (w < 760) && this.headEl && this.cur && !this.cur.loading && !this.el.classList.contains("dragging")) this.paint();
    document.documentElement.classList.toggle("doc-docked", docked);
    this.el.classList.toggle("overlay", !docked);
  }
  undock() { document.documentElement.classList.remove("doc-docked"); }

  step(d) {
    if (!this.docs.length) return;
    const i = this.docs.findIndex((x) => this.cur && x.path === this.cur.path);
    const n = this.docs[(i + d + this.docs.length) % this.docs.length];
    if (n) this.show(n.path);
  }

  async show(path, { keep = false, from = null } = {}) {
    const sid = this.sid, sc = this.scrollEl, y = keep && sc ? sc.scrollTop : 0;
    const [file, hash] = String(path).split("#");
    if (!keep) { this.cur = { path: file, loading: true }; this.paint(); }
    try {
      const q = `path=${encodeURIComponent(file)}` + (from ? `&from=${encodeURIComponent(from)}` : "");
      const d = await get(`sessions/${encodeURIComponent(sid)}/doc?${q}`);
      if (sid !== this.sid) return;
      this.cur = d; this.paint();
      if (keep) { this.scrollEl.scrollTop = y; toast(`${d.name} changed: re-read`); }
      else if (hash) this.jump(hash);
    } catch (e) {
      if (sid !== this.sid) return;
      this.cur = { path: file, error: e.message }; this.paint();
    }
  }

  jump(id) {
    const t = this.bodyEl && this.bodyEl.querySelector(`[id="${CSS.escape(decodeURIComponent(id))}"]`);
    if (t) this.scrollEl.scrollTo({ top: t.offsetTop - 12, behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  }

  paint() {
    const c = this.cur;
    this.headEl = h("div", { class: "doc-head" });
    this.scrollEl = h("div", { class: "doc-scroll", tabindex: "0" });
    this.tocEl = h("nav", { class: "doc-toc", "aria-label": "Contents" });
    this.bodyEl = h("article", { class: "doc-prose", style: `--doc-fs: ${this.size}px` });
    const grip = h("div", { class: "doc-grip", role: "separator", "aria-orientation": "vertical", "aria-label": "Resize the documents panel", tabindex: "0",
      title: "Drag to resize (double-click: default width)", onpointerdown: (e) => this.drag(e), ondblclick: () => { this.w = 0; this.ui.store.set("docsW", 0); this.dock(); },
      onkeydown: (e) => { if (e.key === "ArrowLeft" || e.key === "ArrowRight") { e.preventDefault(); this.w = this.width() + (e.key === "ArrowLeft" ? 40 : -40); this.ui.store.set("docsW", this.w); this.dock(); } } });
    if (!c) this.bodyEl.append(h("div", { class: "empty" }, h("h3", {}, "No documents yet"),
      h("p", {}, "Markdown files this chat makes, or names in a message, show up here. A .md path in any message opens here too.")));
    else if (c.loading) this.bodyEl.append(h("div", { class: "skeleton" }), h("div", { class: "skeleton" }));
    else if (c.error) this.bodyEl.append(h("div", { class: "empty" }, h("h3", {}, "Cannot open this file"), h("p", {}, c.error), h("p", { class: "hint mono" }, c.path)));
    else if (this.raw) this.bodyEl.append(h("pre", { class: "doc-src" }, c.text));
    else this.render(c.text);
    const heads = !this.raw && c && c.text != null ? [...this.bodyEl.querySelectorAll("h1, h2, h3")] : [];
    this.nHeads = heads.length >= 3 ? heads.length : 0;
    const showToc = (this.toc || this.el.classList.contains("narrow")) && this.nHeads > 0;
    this.tocEl.hidden = !showToc;
    if (showToc) this.buildToc(heads);
    this.el.classList.toggle("has-toc", showToc);
    this.scrollEl.append(this.bodyEl);
    this.el.replaceChildren(grip, this.headEl, h("div", { class: "doc-main" }, this.tocEl, this.scrollEl));
    this.paintHead();
    if (!this.el.contains(document.activeElement) || document.activeElement === this.el) this.scrollEl.focus({ preventScroll: true });
  }

  // Header: the switcher (which document, with a searchable list), its facts, and the controls. Repainted alone on a list refresh.
  paintHead() {
    if (!this.headEl) return;
    const c = this.cur, i = c ? this.docs.findIndex((x) => x.path === c.path) : -1;
    const name = c ? (c.name || c.path.split("/").pop()) : "Documents";
    const facts = c && !c.loading && !c.error && c.text != null ? [
      c.written_here ? ["made here", [c.writes && `${c.writes} write${c.writes === 1 ? "" : "s"}`, c.edits && `${c.edits} edit${c.edits === 1 ? "" : "s"}`, c.shell && "by shell"].filter(Boolean).join(", ")].join(": ") : "named in this chat",
      `${Math.max(1, Math.round(c.size / 1024))} KB`, `${Math.max(1, Math.round(c.text.split(/\s+/).length / 230))} min read`, `changed ${age(Date.now() / 1000 - c.mtime)} ago`] : [];
    const btn = (label, title, on, pressed) => h("button", { class: "btn ghost", title, "aria-pressed": pressed == null ? null : String(pressed), onclick: on }, label);
    this.switchBtn = h("button", { class: "doc-switch", "aria-haspopup": "listbox", "aria-expanded": "false", title: "Switch document",
      onclick: () => this.togglePick() }, h("span", { class: "doc-name" }, name), h("span", { class: "doc-caret", "aria-hidden": "true" }, "▾"));
    this.pick = this.buildPick();
    this.headEl.replaceChildren(
      h("div", { class: "doc-row" },
        h("div", { class: "doc-nav" },
          btn("‹", "Previous document ([)", () => this.step(-1)),
          h("span", { class: "hint doc-count" }, this.docs.length ? `${i >= 0 ? i + 1 : "–"} / ${this.docs.length}` : ""),
          btn("›", "Next document (])", () => this.step(1))),
        h("div", { class: "doc-switch-wrap" }, this.switchBtn, this.pick),
        h("div", { class: "doc-actions" },
          btn("A−", "Smaller text", () => this.zoom(-1)), btn("A+", "Larger text", () => this.zoom(1)),
          this.contentsBtn(),
          c && c.text != null && btn("Source", "Show the markdown source", () => { this.raw = !this.raw; this.paint(); }, this.raw),
          c && btn("Copy path", "Copy the full path", () => navigator.clipboard.writeText(c.at_path || c.path).then(() => toast("Path copied"), () => toast(c.at_path || c.path))),
          h("button", { class: "btn ghost doc-x", "aria-label": "Close documents", title: "Close (Esc)", onclick: () => this.close() }, "×"))),
      c && h("div", { class: "doc-meta" }, h("span", { class: "mono doc-path", title: c.at_path || c.path }, c.folder ? `${c.folder}/` : c.path),
        facts.length > 0 && h("span", { class: "hint" }, facts.join(" · "))));
  }

  contentsBtn() {
    // Count the document's headings, not the outline's links: with the outline switched off it has none, and the button
    // disabled itself, so it could never be switched back on.
    const n = this.raw ? 0 : this.nHeads || 0, narrow = this.el.classList.contains("narrow");
    const b = h("button", { class: "btn ghost", disabled: !n, "aria-pressed": String(narrow ? this.el.classList.contains("toc-pop") : this.toc && !!n),
      title: n ? `Outline of ${n} headings${narrow ? " (opens over the text in a narrow panel)" : ""}` : "No outline: this document has fewer than 3 headings",
      onclick: () => {
        if (narrow) { this.el.classList.toggle("toc-pop"); b.setAttribute("aria-pressed", String(this.el.classList.contains("toc-pop"))); return; }
        this.toc = !this.toc; this.ui.store.set("docsToc", this.toc); this.paint();
      } }, "Contents");
    return b;
  }

  buildPick() {
    const c = this.cur, f = this.filter.trim().toLowerCase();
    const match = (d) => !f || d.name.toLowerCase().includes(f) || d.folder.toLowerCase().includes(f);
    const row = (d) => h("li", { role: "option", "aria-selected": String(!!(c && c.path === d.path)) },
      h("button", { class: "doc-opt" + (c && c.path === d.path ? " on" : ""), title: d.path, onclick: () => { this.pick.hidden = true; this.show(d.path); } },
        h("b", {}, d.name),
        h("span", { class: "hint mono" }, d.folder),
        h("span", { class: "hint" }, [!d.exists && "missing", d.shell && !d.writes && !d.edits && "shell", d.by_subagent && "subagent",
          d.last_at && age((Date.now() - Date.parse(d.last_at)) / 1000) + " ago"].filter(Boolean).join(" · "))));
    const made = this.docs.filter((d) => d.kind !== "mentioned" && match(d)), named = this.docs.filter((d) => d.kind === "mentioned" && match(d));
    const list = h("ul", { class: "doc-opts", role: "listbox", "aria-label": "Documents" },
      made.length > 0 && h("li", { class: "doc-group", role: "presentation" }, `Made in this chat · ${made.length}`), made.map(row),
      named.length > 0 && h("li", { class: "doc-group", role: "presentation" }, `Named in this chat · ${named.length}`), named.map(row),
      !made.length && !named.length && h("li", { class: "hint doc-group", role: "presentation" }, this.docs.length ? "Nothing matches" : "No documents in this chat"));
    const find = h("input", { type: "search", class: "field", placeholder: "Filter documents", value: this.filter, "aria-label": "Filter documents",
      oninput: (e) => { this.filter = e.target.value; const nl = this.buildPick().querySelector(".doc-opts"); this.pick.querySelector(".doc-opts").replaceWith(nl); },
      onkeydown: (e) => { if (e.key === "Enter") { const b = this.pick.querySelector(".doc-opt"); if (b) b.click(); } } });
    return h("div", { class: "doc-pick", hidden: true }, find, list);
  }

  togglePick() {
    const open = this.pick.hidden;
    this.pick.hidden = !open; this.switchBtn.setAttribute("aria-expanded", String(open));
    if (open) {
      const f = this.pick.querySelector("input"); f.focus();
      const on = this.pick.querySelector(".doc-opt.on"); if (on) on.scrollIntoView({ block: "nearest" });
      const away = (e) => { if (!this.pick.contains(e.target) && !this.switchBtn.contains(e.target)) { this.pick.hidden = true; this.switchBtn.setAttribute("aria-expanded", "false"); document.removeEventListener("pointerdown", away, true); } };
      document.addEventListener("pointerdown", away, true);
    }
  }

  zoom(d) {
    const i = SIZES.indexOf(this.size);
    this.size = SIZES[Math.max(0, Math.min(SIZES.length - 1, (i < 0 ? 2 : i) + d))]; this.ui.store.set("docsSize", this.size);
    this.bodyEl.style.setProperty("--doc-fs", `${this.size}px`);
  }

  drag(e) {
    if (innerWidth < 760) return;
    e.preventDefault();
    const move = (ev) => { this.w = innerWidth - ev.clientX; this.dock(); };
    const up = () => { removeEventListener("pointermove", move); removeEventListener("pointerup", up); this.el.classList.remove("dragging");
      this.w = this.el.getBoundingClientRect().width; this.ui.store.set("docsW", Math.round(this.w)); };   // store what was drawn, after the cap
    this.el.classList.add("dragging");
    addEventListener("pointermove", move); addEventListener("pointerup", up);
  }

  // Markdown to safe DOM: marked (GFM: tables, task lists, strikethrough, autolinks), DOMPurify, then the reader's touches.
  render(text) {
    let src = text, fm = null;
    const m = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?/.exec(src);
    if (m) {
      fm = m[1].split(/\r?\n/).map((l) => /^(\s*)([\w.-]+):\s?(.*)$/.exec(l)).filter(Boolean);
      src = src.slice(m[0].length);
    }
    const tpl = document.createElement("template");
    tpl.innerHTML = DOMPurify.sanitize(marked.parse(src), { USE_PROFILE: { html: true }, FORBID_TAGS: ["style", "form", "button"], ADD_ATTR: ["start"] });
    const frag = tpl.content;
    if (fm && fm.length) frag.prepend(h("dl", { class: "doc-fm" }, fm.flatMap(([, ind, k, v]) => [h("dt", { style: ind ? "padding-left: 1em" : null }, k), h("dd", {}, v)])));
    const seen = new Map();
    for (const hd of frag.querySelectorAll("h1, h2, h3, h4, h5, h6")) {
      let id = slug(hd.textContent) || "section"; const n = seen.get(id) || 0; seen.set(id, n + 1); if (n) id += `-${n}`;
      hd.id = id;
      hd.append(h("a", { class: "doc-anchor", href: `#${id}`, "aria-label": "Link to this section", "data-anchor": id }, "#"));
    }
    for (const a of frag.querySelectorAll("a[href]")) {
      const href = a.getAttribute("href");
      if (a.dataset.anchor) continue;
      if (/^https?:\/\//i.test(href) || /^mailto:/i.test(href)) { a.target = "_blank"; a.rel = "noopener noreferrer"; }
      else if (href.startsWith("#")) a.dataset.anchor = href.slice(1);
      else if (/\.md(#.*)?$/i.test(href)) { a.dataset.doc = href; a.title = `Open ${href} here`; }
      else { a.removeAttribute("href"); a.classList.add("doc-deadlink"); a.title = `${href} (only .md files open in the reader)`; }
    }
    for (const img of frag.querySelectorAll("img")) {          // local image paths cannot load in the browser; say what is there
      if (!/^https:\/\//i.test(img.getAttribute("src") || "")) img.replaceWith(h("span", { class: "chip doc-img" }, `image: ${img.getAttribute("alt") || img.getAttribute("src") || ""}`));
      else img.loading = "lazy";
    }
    for (const t of frag.querySelectorAll("table")) { const w = h("div", { class: "doc-table" }); t.replaceWith(w); w.append(t); }
    for (const cb of frag.querySelectorAll("input[type=checkbox]")) { cb.disabled = true; cb.closest("li") && cb.closest("li").classList.add("task"); }
    for (const code of frag.querySelectorAll("pre > code")) {
      const lang = ((code.className.match(/language-([\w+-]+)/) || [])[1] || "").toLowerCase();
      try {
        if (lang && hljs.getLanguage(lang)) code.innerHTML = hljs.highlight(code.textContent, { language: lang, ignoreIllegals: true }).value;   // hljs escapes its input
        else if (!lang && code.textContent.length < 20000) { const r = hljs.highlightAuto(code.textContent, ["bash", "python", "json", "yaml", "javascript", "typescript", "sql", "diff"]); if (r.relevance >= 6) code.innerHTML = r.value; }
      } catch { /* plain text is fine */ }
      code.classList.add("hljs");
      const pre = code.parentElement;
      pre.classList.add("doc-pre");
      pre.prepend(h("div", { class: "doc-pre-bar" }, h("span", {}, lang || ""), h("button", { class: "doc-copy", type: "button", title: "Copy this block",
        onclick: (e) => navigator.clipboard.writeText(code.textContent).then(() => { e.target.textContent = "Copied"; setTimeout(() => (e.target.textContent = "Copy"), 1200); }, () => toast("Copy failed")) }, "Copy")));
    }
    this.bodyEl.replaceChildren(frag);
    this.bodyEl.addEventListener("click", (e) => {
      const a = e.target.closest("a");
      if (!a) return;
      if (a.dataset.anchor) { e.preventDefault(); this.jump(a.dataset.anchor); }
      else if (a.dataset.doc) { e.preventDefault(); this.show(a.dataset.doc, { from: this.cur && this.cur.path }); }
      else if (a.classList.contains("doc-deadlink")) e.preventDefault();
    });
  }

  buildToc(heads) {
    const items = heads.map((hd) => h("a", { href: `#${hd.id}`, class: `lv${hd.tagName[1]}`, onclick: (e) => { e.preventDefault(); this.el.classList.remove("toc-pop"); this.jump(hd.id); } },
      hd.firstChild ? hd.firstChild.textContent : hd.textContent));
    this.tocEl.replaceChildren(h("div", { class: "doc-toc-head" }, "Contents"), ...items);
    let raf = 0;
    const mark = () => {
      raf = 0;
      const top = this.scrollEl.scrollTop + 24;
      let k = 0;
      heads.forEach((hd, j) => { if (hd.offsetTop <= top) k = j; });
      items.forEach((a, j) => a.classList.toggle("on", j === k));
    };
    this.scrollEl.addEventListener("scroll", () => { if (!raf) raf = requestAnimationFrame(mark); }, { passive: true });
    requestAnimationFrame(mark);
  }
}

// A doc link inside a message: a click (or Enter) opens it; inside a tool row it must not also fold the row.
export function onDocLink(root, open) {
  const go = (e) => {
    const a = e.target.closest && e.target.closest(".doc-link");
    if (!a || !root.contains(a)) return false;
    e.preventDefault(); e.stopPropagation(); open(a.dataset.path); return true;
  };
  root.addEventListener("click", go);
  root.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") go(e); });
}
