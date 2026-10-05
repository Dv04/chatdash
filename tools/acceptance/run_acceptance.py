#!/usr/bin/env python3
"""B11 acceptance checklist. Re-runnable:  python3 run_acceptance.py [--live 8787] [--mock 8791] [--only current|nebula]
The 8 checks run twice: the default (current) look, then ?look=nebula (stage 1), followed by the
nebula-only checks: pause rule, data truth, reduced motion, nothing drawn behind the windows, default unchanged.
Drives shoot.mjs (Chrome DevTools protocol, real device emulation) against the live server (real data) and the
mock (synthetic scenarios), scores every criterion, writes REPORT.md and the screenshot set next to this file.
Keyboard checks dispatch real key events into the page (synthetic KeyboardEvents, not OS-level keys)."""
import argparse
import json
import re
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SHOTS = os.path.join(HERE, "final")
WAIT_GRAPH = "let w=0; while(!window.__graph && w<80){await new Promise(r=>setTimeout(r,100)); w++;} const g=window.__graph; let k=0; while((!g.m||!g.m.nodes.length) && k<60){await new Promise(r=>setTimeout(r,100)); k++;}"
KEYS = """(async()=>{try{const sleep=t=>new Promise(r=>setTimeout(r,t)); await sleep(2500);
const key=(k,o={})=>document.dispatchEvent(new KeyboardEvent('keydown',Object.assign({key:k,bubbles:true},o)));
const out={};
key('j'); await sleep(150); out.j_marks_current=!!document.querySelector('.strip.cur');
key('j'); await sleep(150); out.j_moves=document.querySelector('.strip.cur')?.id==='s-1';
key('k'); await sleep(150); out.k_moves_back=document.querySelector('.strip.cur')?.id==='s-0';
key('k',{metaKey:true}); await sleep(250); const pal=document.querySelector('dialog.palette'); out.cmdk_opens_palette=!!(pal&&pal.open); pal&&pal.close();
key('?'); await sleep(150); out.help_toast=document.getElementById('toast').classList.contains('on');
key('g'); await sleep(1500); out.g_opens_graph=location.hash==='#/graph' && !document.getElementById('graphview').hidden;
const g=window.__graph; if(g){ g.canvas.focus(); g.focus=null; g.key({key:'ArrowRight',preventDefault(){}}); out.arrow_focuses_node=!!g.focus;
  g.key({key:'Enter',preventDefault(){}}); out.enter_opens_panel=!g.panel.hidden; g.key({key:'Escape',preventDefault(){}}); out.esc_closes_panel=g.panel.hidden; }
key('g'); await sleep(600); out.g_back_to_board=location.hash==='#/';
const focusables=[...document.querySelectorAll('button,a[href],input,textarea,select,[tabindex]:not([tabindex="-1"])')].filter(e=>e.offsetParent!==null);
out.focusable_controls=focusables.length; out.skip_link=!!document.querySelector('a.skip[href="#main"]');
return JSON.stringify(out);}catch(e){return JSON.stringify({error:String(e)})}})()"""
DIMS = "JSON.stringify({rail: (document.querySelector('.health')||{}).textContent, unknownSeats: document.querySelectorAll('.seat.unknown').length, hatched: document.querySelectorAll('.gauge.unk').length, strips: document.querySelectorAll('#main .strip').length, title: document.title})"


def run_shoot(specs, gl=False):
    p = os.path.join(HERE, ".specs.json")
    json.dump(specs, open(p, "w"))
    env = {**os.environ, "SHOOT_GL": "1" if gl else "0"}
    out = subprocess.run(["node", os.path.join(HERE, "shoot.mjs"), p], capture_output=True, text=True, timeout=900, env=env).stdout
    os.remove(p)
    return [json.loads(l) for l in out.splitlines() if l.strip().startswith("{")]


def scenario(mock, name):
    urllib.request.urlopen(f"http://127.0.0.1:{mock}/mock/scenario?name={name}", timeout=5).read()


RAF_COUNTER = ("window.__raf = 0; const __r = window.requestAnimationFrame.bind(window);"
               "window.requestAnimationFrame = (cb) => __r((t) => { window.__raf++; cb(t); });")
WAIT_NEB = "const sleep=t=>new Promise(r=>setTimeout(r,t)); let w=0; while(!(window.__nebula&&__nebula.model&&__nebula.model.loaded&&__nebula.drawn.length) && w<100){await sleep(100); w++;}"
# Check 4. Positive control first (the loop runs while focused: >= 2 frames in 3 s; software GL is slow, a
# frame can take over a second, so this proves the loop runs, not a frame rate), then blur + hidden: 0 rAF callbacks over 2 s.
# A real data change while frozen (one decision answered through the mock API) must repaint exactly one still frame
# and move the tab title count; focus brings the loop back.
PAUSE = "(async()=>{try{" + WAIT_NEB + """ const out={};
let ww=0; while(__nebula.frames<10 && ww<300){await sleep(100); ww++;} out.warmup_ms=ww*100;
let a=__raf; await sleep(3000); out.focused_frames_3s=__raf-a;
window.dispatchEvent(new Event('blur'));
Object.defineProperty(document,'visibilityState',{configurable:true,get:()=>'hidden'}); Object.defineProperty(document,'hidden',{configurable:true,get:()=>true});
document.dispatchEvent(new Event('visibilitychange'));
a=__raf; await sleep(2000); out.frozen_raf_2s=__raf-a; out.running_after_blur=__nebula.running;
const n0=parseInt((document.title.match(/^\\((\\d+)\\)/)||[])[1]||'0',10); out.title_before=document.title;   // title counts workable needs (parked excluded)
const p0=__nebula.stillPaints;
await fetch('/api/cp/decisions/d-102/answer',{method:'POST',headers:{'X-Token':'mock','Content-Type':'application/json'},body:'{}'});
__cp.refresh(); await sleep(2500);
out.still_paints_after_change=__nebula.stillPaints-p0; out.frozen_raf_incl_change=__raf-a;
out.title_after=document.title; out.title_expected='('+(n0-1)+') '; out.model_needs=__nebula.model.needs;
delete document.visibilityState; delete document.hidden; document.dispatchEvent(new Event('visibilitychange'));
window.dispatchEvent(new Event('focus')); a=__raf; await sleep(3000); out.resumed_frames_3s=__raf-a;
return JSON.stringify(out);}catch(e){return JSON.stringify({error:String(e)})}})()"""
# Check 7, read from the field's own model and the list of what its last paint drew (not a screenshot).
TRUTH = "(async()=>{try{" + WAIT_NEB + """ const m=__nebula.model, d=__nebula.drawn, cs=getComputedStyle(document.documentElement);
const lab=s=>([...document.querySelectorAll('.nb-cl')].find(e=>e.querySelector('b').textContent===s)||{}).textContent||null;
const pick=n=>({model:m.seats.find(x=>x.seat===n), drawn:d.find(x=>x.seat===n), label:lab(n)});
const t=pick('gamma'), h=pick('echo');
return JSON.stringify({gamma:{kind:t.model.kind,left:t.model.left,hatched:t.drawn.hatched,particles:t.drawn.particles,label:t.label},
  echo:{kind:h.model.kind,left:h.model.left,color:h.drawn.color,label:h.label,hatched:h.drawn.hatched,particles:h.drawn.particles},
  outToken:cs.getPropertyValue('--nb-out').trim(), seatToken:cs.getPropertyValue('--nb-seat').trim()});}catch(e){return JSON.stringify({error:String(e)})}})()"""
# Nothing is painted below the band, so the glass windows only ever sit over the CSS void gradient (the backdrop
# contrast.py checks window text against). Positive control: the band itself has painted pixels.
# The field never sits behind the windows, so the glass only ever has the CSS void behind it (what contrast.py checks
# window text against). GL path: the canvas ends above the windows, and every seat callout sits on the clear floor
# the renderer leaves transparent. 2D fallback: pixel read of the canvas below and above the band.
BACKDROP = "(async()=>{" + WAIT_NEB + """ await sleep(800); const f=__nebula.field, win=document.querySelector('#side').getBoundingClientRect();
const wt=Math.round(win.top+scrollY);
if (f.mode==='gl') { const r=f.glCanvas.getBoundingClientRect(), cb=r.bottom+scrollY, zone=f.zone();
  const tops=[...document.querySelectorAll('.nb-cl')].map(e=>e.getBoundingClientRect().top+scrollY);
  return JSON.stringify({mode:'gl', canvas_bottom:Math.round(cb), windows_top:wt, zone, floor_top:Math.round(cb-zone), callouts_min_top:Math.round(Math.min(...tops)), callouts:tops.length}); }
const c=f.canvas, g=c.getContext('2d'); const top=Math.round(f.rect.y*f.dpr), bot=Math.ceil((f.rect.y+f.rect.h)*f.dpr);
const maxA=(y0,y1)=>{ if(y1<=y0) return 0; const im=g.getImageData(0,y0,c.width,y1-y0).data; let m=0; for(let i=3;i<im.length;i+=4) if(im[i]>m) m=im[i]; return m; };
return JSON.stringify({mode:'2d', band_max_alpha:maxA(top,bot), below_band_max_alpha:maxA(bot,c.height), above_band_max_alpha:maxA(0,top),
  band_bottom:Math.round(f.rect.y+f.rect.h), windows_top:wt});})()"""
REDUCED = "(async()=>{" + WAIT_NEB + """ const a=__raf; await sleep(2000); return JSON.stringify({raf_2s:__raf-a, still_paints:__nebula.stillPaints, drawn:__nebula.drawn.length, running:__nebula.running});})()"""
# Sky (#/sky, the user 2026-10-03): opens first in the nebula look, GL renderer, every seat projected on screen, fly-in panel,
# a needs-you item opens its real answer card, and the same pause rule as the board.
SKY = "(async()=>{const sleep=t=>new Promise(r=>setTimeout(r,t)); let w=0; while(!(window.__sky&&__sky.model&&__sky.seats.length&&__sky.seats[0].p) && w<150){await sleep(100); w++;}" + """
const out={view:document.documentElement.dataset.view, gl:!!__sky.gl, seats:__sky.seats.length,
  on_screen:__sky.seats.filter(s=>s.p&&s.p.x>0&&s.p.x<innerWidth&&s.p.y>0&&s.p.y<innerHeight).length, callouts:document.querySelectorAll('.sky-cl').length};
let k=0; while(__sky.frames<6 && k<300){await sleep(100); k++;} let a=__raf; await sleep(3000); out.focused_frames_3s=__raf-a;
const it=__cp.overview().needs_you.find(x=>__sky.seats.some(s=>s.seat===x.seat)); __sky.flyTo(it.seat); await sleep(400);
out.panel=!__sky.panel.hidden; out.panel_has_7d=/7d/i.test(__sky.panel.textContent); __sky.openAsk(it); await sleep(300);
out.ask=!__sky.ask.hidden; out.ask_controls=__sky.ask.querySelectorAll('.opt, textarea, input').length;
window.dispatchEvent(new Event('blur')); a=__raf; await sleep(2000); out.frozen_raf_2s=__raf-a; out.running_after_blur=!!__sky.raf;
return JSON.stringify(out);})()"""
DEFAULT = "JSON.stringify({look:document.documentElement.dataset.look||null, canvas_hidden:document.querySelector('.nb-canvas').hidden, capsule:getComputedStyle(document.querySelector('.nb-capsule')).display, rail_nav:getComputedStyle(document.querySelector('.rail .nav')).display})"


def suite(a, look):
    """The 8 B11 checks for one look. look: None (current, the default) or "nebula" (?look=nebula)."""
    q = "?look=nebula" if look else ""
    tag = " [nebula]" if look else ""
    pre = "nebula-" if look else ""
    L, M = f"http://127.0.0.1:{a.live}/v2/{q}", f"http://127.0.0.1:{a.mock}/v2/{q}"
    S = lambda n: os.path.join(SHOTS, pre + n)
    Lb, Mb = (L + "#/", M + "#/") if look else (L, M)    # the nebula look opens on Sky: board checks address the board
    checks = []
    foc = bool(look)              # nebula specs emulate a focused window so the field runs as it would on screen

    # 1. real data: desk light/dark, phone, graph, work item; 390 px overflow; external requests
    scenario(a.mock, "normal")
    gl = bool(look)               # nebula runs render through software WebGL2 so the GL path is what gets checked
    real = run_shoot([
        {"url": Lb, "out": S("board-desk.png"), "width": 1440, "height": 1000, "wait": 4000, "eval": DIMS, "focus": foc},
        {"url": Lb, "out": S("board-desk-dark.png"), "width": 1440, "height": 1000, "dark": True, "wait": 4000, "focus": foc},
        {"url": Lb, "out": S("board-phone.png"), "width": 390, "height": 844, "mobile": True, "scale": 2, "wait": 4000, "focus": foc},
        {"url": L + "#/graph", "out": S("graph-desk.png"), "width": 1440, "height": 900, "wait": 1000,
         "eval": "(async()=>{" + WAIT_GRAPH + " await new Promise(r=>setTimeout(r,4500)); g.fit(); return g.m.nodes.length})()"},
        {"url": L + "#/graph", "out": S("graph-phone.png"), "width": 390, "height": 844, "mobile": True, "scale": 2, "wait": 5000},
        {"url": L + "#/work/PROJ-08", "out": S("workitem-desk.png"), "width": 1440, "height": 1000, "wait": 4000},
        {"url": L + "#/work/PROJ-08", "out": S("workitem-phone.png"), "width": 390, "height": 844, "mobile": True, "scale": 2, "wait": 4000},
    ], gl)
    phones = [r for r in real if r["width"] == 390]
    checks.append(("390 px: no horizontal scroll (board, graph, work item; real data)" + tag,
                   all(r["innerWidth"] == 390 and r["scrollWidth"] == 390 for r in phones),
                   ", ".join(f"{os.path.basename(r['out'])} {r['scrollWidth']}px" for r in phones)))
    ext = sorted({u for r in real for u in r["external"]})
    checks.append(("Zero external requests (network log, all real-data pages)" + tag, not ext, f"{sum(r['requests'] for r in real)} requests, external: {ext or 'none'}"))
    errs = [e for r in real for e in r["errors"]]
    checks.append(("No console errors (real data)" + tag, not errs, "; ".join(errs[:3]) or "none"))

    # 2. keyboard only
    kb = run_shoot([{"url": Mb, "out": S("keyboard.png"), "width": 1440, "height": 900, "wait": 500, "eval": KEYS, "focus": foc}], gl)[0]
    k = json.loads(kb.get("eval") or "{}")
    kb_ok = all(v for key, v in k.items() if key not in ("focusable_controls",)) and k.get("focusable_controls", 0) > 10
    checks.append(("Keyboard-only: j/k, Cmd+K, ?, g, arrows, Enter, Esc, skip link" + tag, kb_ok, json.dumps(k)))

    # 3. offline shell
    off = run_shoot([{"url": Lb, "out": S("offline.png"), "width": 1440, "height": 900, "wait": 4000, "offlineReload": True, "offlineWait": 25000,
                      "eval": "JSON.stringify({rail:(document.querySelector('.health')||{}).textContent, title:document.title, look:document.documentElement.dataset.look||null})"}], gl)[0]
    o = json.loads(off.get("eval") or "{}")
    checks.append(("Offline: cached shell opens and says UNKNOWN (never green)" + tag, o.get("rail") == "UNKNOWN" and (o.get("look") == look), json.dumps(o)))

    # 4. missing data shows amber
    scenario(a.mock, "missing")
    miss = run_shoot([{"url": Mb, "out": S("missing-data.png"), "width": 1440, "height": 900, "wait": 3500, "focus": foc,
                       "eval": DIMS if not look else "(async()=>{" + WAIT_NEB + " const d=JSON.parse(" + DIMS + "); d.field_unknown=__nebula.model.seats.filter(s=>s.kind==='unknown').length; d.field_hatched=__nebula.drawn.filter(x=>x.hatched).length; d.field_note=(document.querySelector('.nb-note')||{}).textContent||null; return JSON.stringify(d)})()"}], gl)[0]
    m = json.loads(miss.get("eval") or "{}")
    ok = m.get("rail") == "UNKNOWN" and m.get("unknownSeats") == 7 and m.get("hatched", 0) >= 14
    if look:
        ok = ok and m.get("field_unknown") == 7 and m.get("field_hatched") == 7 and bool(m.get("field_note"))
    checks.append(("Missing data shows amber: meter unreadable -> UNKNOWN rail, 7 dashed seats, hatched gauges" +
                   (", 7 hatched unknown clusters and a field note" if look else "") + tag, ok, json.dumps(m)))

    # 5. 100-node graph at 60 fps
    scenario(a.mock, "big")
    big = run_shoot([{"url": M + "#/graph", "out": S("graph-100-nodes.png"), "width": 1440, "height": 900, "wait": 500,
                      "eval": "(async()=>{" + WAIT_GRAPH + " g.expanded=new Set(g.m.nodes.filter(n=>n.type==='cluster').map(n=>n.group)); g.relayout(); g.alpha=1; g.frames=[]; g.kick(); await new Promise(r=>setTimeout(r,3000)); const out={fps:g.fps(),frames:g.frames.length,nodes:g.m.nodes.length,edges:g.m.edges.length}; g.fit(); await new Promise(r=>setTimeout(r,300)); return JSON.stringify(out)})()"}])[0]
    b = json.loads(big.get("eval") or "{}")
    checks.append(("Graph: 100+ nodes at 60 fps while the layout moves" + tag, b.get("nodes", 0) >= 100 and (b.get("fps") or 0) >= 58, json.dumps(b)))
    scenario(a.mock, "normal")

    # 6. contrast AA (both themes; app.css tokens and the nebula glass over the field)
    c = subprocess.run([sys.executable, os.path.join(HERE, "contrast.py")], capture_output=True, text=True)
    lines = c.stdout.strip().splitlines()
    checks.append(("Contrast AA, both themes, every text/control token pair" + (" incl. nebula glass over the field" if look else "") + tag,
                   c.returncode == 0, lines[-1] if lines else c.stderr[-200:]))
    return checks


def nebula_only(a):
    M = f"http://127.0.0.1:{a.mock}/v2/"
    S = lambda n: os.path.join(SHOTS, "nebula-" + n)
    checks = []
    scenario(a.mock, "normal")
    r = run_shoot([
        {"url": M + "?look=nebula#/", "out": S("pause.png"), "width": 1440, "height": 900, "wait": 500, "focus": True, "preScript": RAF_COUNTER, "eval": PAUSE},
        {"url": M + "?look=nebula#/", "out": S("backdrop.png"), "width": 1440, "height": 900, "dark": True, "wait": 500, "focus": True, "eval": BACKDROP},
        {"url": M + "?look=nebula#/", "out": S("reduced.png"), "width": 1440, "height": 900, "dark": True, "wait": 500, "focus": True, "reducedMotion": True,
         "preScript": RAF_COUNTER, "eval": REDUCED},
        {"url": M, "out": S("default-look.png"), "width": 1440, "height": 900, "wait": 2500, "eval": DEFAULT},
    ], True)
    p = json.loads(r[0].get("eval") or "{}")
    checks.append(("Pause: blur + hidden = 0 frames over 2 s; a data change while frozen repaints 1 still frame and the title count moves",
                   p.get("focused_frames_3s", 0) >= 2 and p.get("frozen_raf_2s") == 0 and p.get("frozen_raf_incl_change") == 0
                   and p.get("running_after_blur") is False and p.get("still_paints_after_change") == 1
                   and str(p.get("title_after", "")).startswith(p.get("title_expected", "?")) and p.get("resumed_frames_3s", 0) >= 2, json.dumps(p)))
    bd = json.loads(r[1].get("eval") or "{}")
    bd_ok = (bd.get("mode") == "gl" and bd.get("canvas_bottom", 1e9) <= bd.get("windows_top", 0) and bd.get("callouts") == 7
             and bd.get("zone", 0) > 0 and bd.get("callouts_min_top", 0) >= bd.get("floor_top", 1e9))
    checks.append(("Field (WebGL2 path) stays above the windows; seat callouts sit on its transparent floor (the contrast backdrop holds)", bd_ok, json.dumps(bd)))
    rd = json.loads(r[2].get("eval") or "{}")
    checks.append(("Reduced motion: still field (0 frames over 2 s), painted with data", rd.get("raf_2s") == 0 and rd.get("still_paints", 0) >= 1
                   and rd.get("drawn") == 7 and rd.get("running") is False, json.dumps(rd)))
    d = json.loads(r[3].get("eval") or "{}")
    checks.append(("Default look unchanged: no ?look and no stored choice = current look, field off",
                   d.get("look") is None and d.get("canvas_hidden") is True and d.get("capsule") == "none" and d.get("rail_nav") != "none", json.dumps(d)))
    sk = json.loads(run_shoot([{"url": M + "?look=nebula", "out": S("sky.png"), "width": 1440, "height": 900, "dark": True, "wait": 500, "focus": True,
                                "preScript": RAF_COUNTER, "eval": SKY}], True)[0].get("eval") or "{}")
    checks.append(("Sky: opens first, WebGL2, all 7 seats on screen, fly-in panel, a question opens its answer card, 0 frames when blurred",
                   sk.get("view") == "sky" and sk.get("gl") and sk.get("seats") == 7 and sk.get("on_screen") == 7 and sk.get("callouts") == 7
                   and sk.get("focused_frames_3s", 0) >= 2 and sk.get("panel") and sk.get("panel_has_7d") and sk.get("ask") and sk.get("ask_controls", 0) >= 1
                   and sk.get("frozen_raf_2s") == 0 and sk.get("running_after_blur") is False, json.dumps(sk)))
    day = run_shoot([
        {"url": M + "?look=nebula#/", "out": S("day-board.png"), "width": 1440, "height": 900, "wait": 6000, "focus": True,
         "eval": "JSON.stringify({mode:__nebula.field.mode, dark:__nebula.field.col.dark, window:getComputedStyle(document.querySelector('.nb-field'),'::before').backgroundImage.startsWith('radial'), clip:getComputedStyle(document.querySelector('.nb-field > canvas')).clipPath.startsWith('inset'), page:getComputedStyle(document.documentElement).backgroundColor})"},
        {"url": M + "?look=nebula", "out": S("day-sky.png"), "width": 1440, "height": 900, "wait": 7000, "focus": True,
         "eval": "JSON.stringify({gl:!!__sky.gl, err:__sky.glError||null, dark:__sky.col?__sky.col.dark:null})"}], True)
    db, ds = json.loads(day[0].get("eval") or "{}"), json.loads(day[1].get("eval") or "{}")
    derr = [e for r in day for e in r["errors"]]
    # Night window: in the light theme the board field is a night-sky window in a pale page, Sky is night
    light_page = (lambda m: bool(m) and float(m.group(1)) > 0.85)(re.search(r"oklch\(([\d.]+)", db.get("page", "")))
    checks.append(("Light theme (Night window): board field is a clipped night window (WebGL2) on a light page, Sky is night, no console errors",
                   db.get("mode") == "gl" and db.get("dark") is True and db.get("window") is True and db.get("clip") is True and light_page
                   and ds.get("gl") is True and ds.get("dark") is True and not derr,
                   json.dumps({"board": db, "sky": ds, "errors": derr[:2]})))
    scenario(a.mock, "normal")
    scenario(a.mock, "truth")
    t = run_shoot([{"url": M + "?look=nebula#/", "out": S("truth.png"), "width": 1440, "height": 900, "dark": True, "wait": 500, "focus": True, "eval": TRUTH}], True)[0]
    scenario(a.mock, "normal")
    tr = json.loads(t.get("eval") or "{}")
    ty, he = tr.get("gamma", {}), tr.get("echo", {})
    checks.append(("Data truth: no-reading seat = hatched 'unknown' cluster (no particles); 98% seat = nearly-out hue and label",
                   ty.get("kind") == "unknown" and ty.get("left") is None and ty.get("hatched") is True and ty.get("particles") == 0 and "unknown" in (ty.get("label") or "")
                   and he.get("kind") == "out" and he.get("color") == tr.get("outToken") and he.get("color") != tr.get("seatToken")
                   and "nearly out" in (he.get("label") or "") and "2% left" in (he.get("label") or "") and he.get("hatched") is False, json.dumps(tr)))
    return checks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", type=int, default=8787)
    ap.add_argument("--mock", type=int, default=8791)
    ap.add_argument("--only", choices=["current", "nebula"])
    a = ap.parse_args()
    os.makedirs(SHOTS, exist_ok=True)
    checks = []
    if a.only != "nebula":
        checks += suite(a, None)
    if a.only != "current":
        checks += suite(a, "nebula") + nebula_only(a)

    when = time.strftime("%Y-%m-%d %H:%M") + " CT"
    md = ["# chatdash v2 acceptance (B11 + nebula stage 1)\n", f"Run {when} by run_acceptance.py; live server {a.live} (real data), mock {a.mock} (synthetic).\n",
          "| Check | Result | Evidence |", "|---|---|---|"]
    for name, ok, ev in checks:
        md.append(f"| {name} | {'PASS' if ok else 'FAIL'} | {str(ev).replace('|', '/')[:400]} |")
    md += ["", "Screenshots: final/*.png (board desk light and dark, phone, graph desk and phone, work item, keyboard run, offline, missing data, 100-node graph);",
           "final/nebula-*.png the same set with ?look=nebula, plus the pause, backdrop, reduced-motion, default-look and data-truth runs.",
           "Keyboard events are dispatched inside the page (synthetic), not OS-level key presses. Nebula runs emulate a focused window",
           "(headless targets are unfocused, which freezes the field by design). Frame rate (Check 5) is measured separately in real Chrome."]
    open(os.path.join(HERE, "REPORT.md"), "w").write("\n".join(md) + "\n")
    for name, ok, ev in checks:
        print(("PASS " if ok else "FAIL ") + name + "  :: " + str(ev)[:220])
    print(f"{sum(ok for _, ok, _ in checks)}/{len(checks)} passed")
    sys.exit(0 if all(ok for _, ok, _ in checks) else 1)


if __name__ == "__main__":
    main()
