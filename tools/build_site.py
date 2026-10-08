#!/usr/bin/env python3
"""Builds the static site for orbit.dhi-tech.com into site/ (landing page plus the README as the docs page).

    python3 tools/build_site.py        needs the `markdown` package at build time only; the site itself is plain HTML.
"""
import re, shutil, pathlib, markdown

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC, OUT = ROOT / "site-src", ROOT / "site"
VERSION = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M).group(1)
GH = "https://github.com/Dv04/dhi-orbit"

if OUT.exists():
    shutil.rmtree(OUT)
(OUT / "img").mkdir(parents=True)
(OUT / "docs").mkdir()
for f in (ROOT / "docs" / "screenshots").iterdir():
    shutil.copy(f, OUT / "img" / f.name)
shutil.copy(ROOT / "docs/brand/orbit-app-icon.svg", OUT / "icon.svg")
shutil.copy(ROOT / "docs/brand/orbit-lockup-dark.png", OUT / "img" / "lockup-dark.png")
shutil.copy(SRC / "style.css", OUT / "style.css")
for n in ("github", "paypal", "venmo"):
    shutil.copy(ROOT / "docs/brand" / f"sponsor-{n}.svg", OUT / "img" / f"sponsor-{n}.svg")

HEAD = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><meta name="description" content="{desc}">
<link rel="icon" href="/icon.svg" type="image/svg+xml"><link rel="stylesheet" href="/style.css">
<meta property="og:title" content="{title}"><meta property="og:description" content="{desc}"><meta property="og:image" content="https://orbit.dhi-tech.com/img/board.png"><meta property="og:type" content="website">
</head><body>"""
NAV = f"""<div class="support"><div class="wrap"><span>DHI Orbit is free and open source. Support its development:</span>
<a href="https://github.com/sponsors/Dv04"><img src="/img/sponsor-github.svg" alt="GitHub Sponsors" height="30"></a><a href="https://www.paypal.com/paypalme/DevSanghvi48"><img src="/img/sponsor-paypal.svg" alt="PayPal" height="30"></a><a href="https://account.venmo.com/u/Dev-Sanghvi-1"><img src="/img/sponsor-venmo.svg" alt="Venmo" height="30"></a></div></div>
<div class="wrap"><nav><a class="brand" href="/"><img src="/icon.svg" alt="">DHI Orbit</a>
<div class="links"><a href="/#install">Install</a><a href="/docs/">Docs</a><a href="{GH}">GitHub</a><a href="{GH}/discussions">Discussions</a><a class="sp" href="https://github.com/sponsors/Dv04">Sponsor</a></div></nav></div>"""
FOOT = f"""<footer><div class="wrap"><div class="row"><div>DHI Orbit {VERSION}, MIT licence. Built by Dev Sanghvi at <a href="https://dhi-tech.com">DHI</a>.</div>
<div><a href="{GH}">GitHub</a> · <a href="https://pypi.org/project/dhi-orbit/">PyPI</a> · <a href="{GH}/discussions">Discussions</a> · <a href="{GH}/issues">Issues</a> · <a href="https://github.com/sponsors/Dv04">Sponsor</a></div></div></div></footer>"""

INDEX = HEAD.format(title="DHI Orbit: one local board for every coding-agent chat",
    desc="A local, open-source dashboard for Claude Code, Codex, Cursor, Gemini CLI and Antigravity chats: what needs you, what is working, your limits, and a reply box.") + NAV + f"""
<header class="hero"><div class="wrap">
<h1>One board for every coding-agent chat</h1>
<p class="sub">See which chat is waiting on you, what is working, and each account's 5-hour and 7-day limits. Reply from the page. It runs on your own computer, costs zero model tokens to look at, and is open source.</p>
<div class="cta"><a class="btn p" href="#install">Install</a><a class="btn" href="/docs/">Read the docs</a><a class="btn" href="{GH}">View on GitHub</a><a class="btn sp" href="https://github.com/sponsors/Dv04">Sponsor</a></div>
<div class="inst" id="install"><div class="tabs" role="tablist">
<button role="tab" aria-selected="true" data-t="brew">Homebrew</button><button role="tab" aria-selected="false" data-t="pipx">pipx</button><button role="tab" aria-selected="false" data-t="pip">pip</button></div>
<pre data-p="brew"><code>brew install Dv04/dhi-orbit/dhi-orbit
dhi-orbit</code></pre><pre data-p="pipx" hidden><code>pipx install dhi-orbit
dhi-orbit</code></pre><pre data-p="pip" hidden><code>python3 -m pip install dhi-orbit
dhi-orbit</code></pre>
<p style="color:var(--mute);font-size:15px;margin:10px 0 0">Then open <code>http://127.0.0.1:8787/</code>. Python 3.10 or newer, and at least one supported agent. macOS and Linux; Windows is in beta.</p></div>
<div class="hero-shot"><img src="/img/board.png" alt="The Orbit board: what needs you, with each account's 5-hour and 7-day limits (synthetic demo data)"></div>
</div></header>

<section><div class="wrap"><h2>All your agents, one list</h2>
<p class="lead">Chats from every tool sit in the same lists, labelled with the tool's name. Anything else with a command line can be added with a small <code>agents.json</code>, no code.</p>
<div class="agents"><span>Claude Code</span><span>Codex</span><span>Cursor</span><span>Gemini CLI</span><span>Antigravity</span><span>Any terminal agent</span></div></div></section>

<section><div class="wrap"><h2>What it does</h2><div class="grid" style="margin-top:22px">
<div class="card"><h3>What needs you</h3><p>Questions, permission prompts, blocked jobs and chats stalled on a limit, oldest and most urgent first.</p></div>
<div class="card"><h3>Reply from the page</h3><p>Answer a prompt with the same options the terminal shows, or type a reply. Background chats take replies too.</p></div>
<div class="card"><h3>Limits, never a guess</h3><p>5-hour and 7-day usage per account. When there is no reading it shows "?", not 0%.</p></div>
<div class="card"><h3>Graph of the work</h3><p>Accounts, chats, work items and the files they touch, plus an optional nebula look for a second monitor.</p></div>
<div class="card"><h3>Local and private</h3><p>Binds to 127.0.0.1, every call needs a local token, no hosted version. Standard library only, no build step, no CDN.</p></div>
<div class="card"><h3>Zero tokens to look</h3><p>Viewing only reads files. A reply you send is one normal turn in that chat, through the agent's own CLI.</p></div>
</div></div></section>

<section><div class="wrap"><h2>Screenshots</h2><p class="lead">Synthetic demo data, no real chats.</p><div class="shots">
<img class="wide" src="/img/nebula-board.jpg" alt="The optional nebula look" loading="lazy">
<img src="/img/graph.png" alt="Graph of accounts, chats and work items" loading="lazy"><img src="/img/board-light.png" alt="Light theme" loading="lazy">
</div></div></section>

<section><div class="wrap"><h2>Free and open source</h2>
<p class="lead">DHI Orbit is MIT licensed and stays free. If it saves you time, you can support its upkeep through <a href="https://github.com/sponsors/Dv04">GitHub Sponsors</a>, <a href="https://www.paypal.com/paypalme/DevSanghvi48">PayPal</a> or <a href="https://account.venmo.com/u/Dev-Sanghvi-1">Venmo</a>. Questions and ideas go to <a href="{GH}/discussions">GitHub Discussions</a>.</p></div></section>
""" + FOOT + """<script>
document.querySelectorAll('.tabs button').forEach(b=>b.addEventListener('click',()=>{document.querySelectorAll('.tabs button').forEach(x=>x.setAttribute('aria-selected',x===b));document.querySelectorAll('.inst pre').forEach(p=>p.hidden=p.dataset.p!==b.dataset.t)}));
</script></body></html>"""
(OUT / "index.html").write_text(INDEX)

md = (ROOT / "README.md").read_text()
md = re.sub(r'^<p align="center">.*?</p>\n', "", md, count=1, flags=re.S)
md = md.replace("https://raw.githubusercontent.com/Dv04/dhi-orbit/main/docs/screenshots/", "/img/")
body = markdown.markdown(md, extensions=["fenced_code", "tables", "toc"])
DOCS = HEAD.format(title="DHI Orbit documentation", desc="Install, connect accounts, usage limits, other agents, phone access and the security model for DHI Orbit.") + NAV + '<main class="doc">' + body + "</main>" + FOOT + "</body></html>"
(OUT / "docs" / "index.html").write_text(DOCS)
(OUT / "_headers").write_text("/*\n  X-Content-Type-Options: nosniff\n  Referrer-Policy: strict-origin-when-cross-origin\n")
print("built", VERSION, sorted(p.name for p in OUT.rglob("*") if p.is_file()))
