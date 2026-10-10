// Markdown for Claude's messages: the subset chats actually use (paragraphs, **bold**, *italic*, `code`, fenced
// code, headings, lists, tables, quotes, rules, links). Builds DOM nodes, never innerHTML, so a transcript can never
// inject markup; links open only for http(s). A path to a .md file (absolute, ~/, or relative inside `code`) becomes a
// .doc-link: the chat view opens it in its Docs reader (docs.js); anywhere else it is plain text with a dotted underline.
import { h } from "./lib.js";

const DOC_PATH = /^(?:~\/|\/|\.{0,2}\/?)?[\w.@+\-\/]*\.md(?::\d+)?$/;
const docLink = (path, kid) => h("a", { class: "doc-link", "data-path": path, role: "button", tabindex: "0", title: `Open ${path} in the reader` }, kid);

const INLINE = /(`+)([\s\S]*?[^`])\1(?!`)|\*\*([^*]+(?:\*(?!\*)[^*]*)*)\*\*|__([^_]+)__|~~([^~]+)~~|\*([^*\s][^*]*?)\*|(?<![\w])_([^_\s][^_]*?)_(?![\w])|\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)|(https?:\/\/[^\s<>()]+[^\s<>().,;:!?'"])|(?<![\w\/.~])((?:~\/|\/)[\w.@+\-\/]*\.md(?::\d+)?)(?![\w\/])/g;

export function inline(text) {
  const out = [], re = new RegExp(INLINE.source, "g");      // own regex per call: inline() recurses for **x**
  let last = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    if (m[2] != null) out.push(DOC_PATH.test(m[2].trim()) ? docLink(m[2].trim(), h("code", {}, m[2])) : h("code", {}, m[2]));
    else if (m[3] != null) out.push(h("strong", {}, inline(m[3])));
    else if (m[4] != null) out.push(h("strong", {}, inline(m[4])));
    else if (m[5] != null) out.push(h("del", {}, inline(m[5])));
    else if (m[6] != null) out.push(h("em", {}, inline(m[6])));
    else if (m[7] != null) out.push(h("em", {}, inline(m[7])));
    else if (m[8] != null) out.push(h("a", { href: m[9], target: "_blank", rel: "noopener noreferrer" }, inline(m[8])));
    else if (m[10] != null) out.push(h("a", { href: m[10], target: "_blank", rel: "noopener noreferrer" }, m[10]));
    else if (m[11] != null) out.push(docLink(m[11], m[11]));
    last = re.lastIndex;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

const LIST = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
const cells = (l) => l.trim().replace(/^\||\|$/g, "").split(/(?<!\\)\|/).map((c) => c.trim());

// opts.headings: real heading levels (# -> h2 ... ) for a document; chat messages keep the small h4/h5.
export function md(text, cls = "md", opts = {}) {
  const root = h("div", { class: cls });
  const lines = String(text || "").replace(/\r\n?/g, "\n").split("\n");
  let i = 0, para = [];
  const flush = () => {
    if (!para.length) return;
    const p = h("p", {});
    para.forEach((l, k) => { if (k) p.append(h("br", {})); p.append(...inline(l)); });
    root.append(p); para = [];
  };
  while (i < lines.length) {
    const l = lines[i];
    const fence = l.match(/^\s*(```+|~~~+)\s*([\w+-]*)\s*$/);
    if (fence) {
      flush();
      const body = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith(fence[1])) body.push(lines[i++]);
      i++;
      root.append(h("pre", { class: "code" }, h("code", { "data-lang": fence[2] || "" }, body.join("\n"))));
      continue;
    }
    if (!l.trim()) { flush(); i++; continue; }
    const hd = l.match(/^(#{1,6})\s+(.*)$/);
    if (hd) { flush(); root.append(h(opts.headings ? "h" + Math.min(6, hd[1].length + 1) : hd[1].length <= 2 ? "h4" : "h5", {}, inline(hd[2].replace(/\s#+\s*$/, "")))); i++; continue; }
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(l)) { flush(); root.append(h("hr", {})); i++; continue; }
    if (/^\s*\|.*\|\s*$/.test(l) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(lines[i + 1])) {
      flush();
      const head = cells(l);
      i += 2;
      const rows = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) rows.push(cells(lines[i++]));
      root.append(h("div", { class: "md-table" }, h("table", {},
        h("thead", {}, h("tr", {}, head.map((c) => h("th", {}, inline(c))))),
        h("tbody", {}, rows.map((r) => h("tr", {}, head.map((_, k) => h("td", {}, inline(r[k] || "")))))))));
      continue;
    }
    if (/^\s*>/.test(l)) {
      flush();
      const q = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) q.push(lines[i++].replace(/^\s*>\s?/, ""));
      root.append(h("blockquote", {}, md(q.join("\n"), "md-q", opts)));
      continue;
    }
    if (LIST.test(l)) {
      flush();
      // one list (and nested lists by indent); continuation lines join the item above
      const stack = [];
      while (i < lines.length && (LIST.test(lines[i]) || (stack.length && /^\s{2,}\S/.test(lines[i])))) {
        const m = lines[i].match(LIST);
        if (!m) { const li = stack[stack.length - 1].list.lastChild; li.append(h("br", {}), ...inline(lines[i].trim())); i++; continue; }
        const ind = m[1].replace(/\t/g, "  ").length, ordered = /\d/.test(m[2]);
        while (stack.length && ind < stack[stack.length - 1].ind) stack.pop();
        let top = stack[stack.length - 1];
        if (top && ind === top.ind && (top.list.tagName === "OL") !== ordered) { stack.pop(); top = stack[stack.length - 1]; if (top && ind <= top.ind) top = null; }
        if (!top || ind > top.ind) {
          const list = h(ordered ? "ol" : "ul", ordered ? { start: String(parseInt(m[2], 10)) } : {});
          if (top && top.list.lastChild) top.list.lastChild.append(list); else root.append(list);
          top = { ind, list }; stack.push(top);
        }
        top.list.append(h("li", {}, inline(m[3])));
        i++;
      }
      continue;
    }
    para.push(l);
    i++;
  }
  flush();
  return root;
}
