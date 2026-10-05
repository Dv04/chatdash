#!/usr/bin/env node
// Check 5: nebula frame time in REAL Chrome on this Mac (GPU on, headed window, no --disable-gpu).
//   node fps.mjs [url] [seconds]      default http://127.0.0.1:8787/v2/?look=nebula&fps=1, 30 s
// Opens a separate Chrome instance (temp profile) WITHOUT activating it (open -g), so it never takes keyboard focus
// from the user. Focus is emulated through CDP so the page runs as it does when focused; occluded-window throttling
// is switched off so a window behind others still renders. Sizes the window so innerWidth x innerHeight = 1440 x 900,
// reads the page's own frame-time samples (field.js, enabled by fps=1) over the window, then quits that instance.
// Prints one JSON line: median and p95 frame ms, sample count, viewport, dpr, and the GPU renderer string.
import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const url = process.argv[2] || "http://127.0.0.1:8787/v2/?look=nebula&fps=1";
const secs = Number(process.argv[3] || 30);
const profile = mkdtempSync(join(tmpdir(), "cp2-fps-"));
const port = 9800 + Math.floor(Math.random() * 150);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
spawnSync("open", ["-g", "-n", "-a", "Google Chrome", "--args", `--user-data-dir=${profile}`, `--remote-debugging-port=${port}`,
  "--no-first-run", "--no-default-browser-check", "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
  "--disable-background-timer-throttling", "--window-position=20,20", "--window-size=1440,940", `--app=${url}`]);  // app window: no tab strip, so a 900 px viewport fits this display

async function json(path) {
  for (let i = 0; i < 80; i++) { try { return await (await fetch(`http://127.0.0.1:${port}${path}`)).json(); } catch { await sleep(150); } }
  throw new Error("chrome did not start");
}
function session(wsUrl) {
  const ws = new WebSocket(wsUrl);
  let id = 0;
  const waiting = new Map();
  ws.onmessage = (m) => { const msg = JSON.parse(m.data); if (msg.id && waiting.has(msg.id)) { const { ok, no } = waiting.get(msg.id); waiting.delete(msg.id); msg.error ? no(new Error(msg.error.message)) : ok(msg.result); } };
  return new Promise((res) => ws.onopen = () => res({
    send: (method, params = {}, sessionId) => new Promise((ok, no) => { const i = ++id; waiting.set(i, { ok, no }); ws.send(JSON.stringify({ id: i, method, params, sessionId })); }),
    close: () => ws.close(),
  }));
}

let b;
try {
  const v = await json("/json/version");
  b = await session(v.webSocketDebuggerUrl);
  let page;
  for (let i = 0; i < 40 && !page; i++) { page = (await json("/json/list")).find((t) => t.type === "page" && t.url.startsWith(url.split("?")[0])); if (!page) await sleep(250); }
  const { sessionId } = await b.send("Target.attachToTarget", { targetId: page.id, flatten: true });
  const S = (m, p) => b.send(m, p, sessionId);
  const E = async (expr) => (await S("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true })).result.value;
  await S("Runtime.enable"); await S("Page.enable");
  await S("Emulation.setFocusEmulationEnabled", { enabled: true });
  // size the window so the viewport is exactly 1440 x 900 CSS px
  const { windowId } = await b.send("Browser.getWindowForTarget", { targetId: page.id });
  await b.send("Browser.setWindowBounds", { windowId, bounds: { windowState: "normal" } });
  await sleep(600);
  for (let i = 0; i < 5; i++) {
    const [iw, ih] = JSON.parse(await E("JSON.stringify([innerWidth, innerHeight])"));
    const { bounds } = await b.send("Browser.getWindowBounds", { windowId });
    if (iw === 1440 && ih === 900) break;
    await b.send("Browser.setWindowBounds", { windowId, bounds: { left: 20, top: 20, width: bounds.width + 1440 - iw, height: bounds.height + 900 - ih } });
    await sleep(400);
  }
  const vp = JSON.parse(await E("JSON.stringify([innerWidth, innerHeight])"));
  if (vp[0] !== 1440 || vp[1] !== 900) throw new Error(`viewport is ${vp.join("x")}, not 1440x900: not a valid check 5 run`);
  await S("Page.reload", {});
  await sleep(5000);
  await E("(async()=>{let w=0; while(!(window.__nebula&&__nebula.running) && w<100){await new Promise(r=>setTimeout(r,100)); w++;}})()");
  await E("__nebula.field.dts.length = 0");
  const f0 = await E("__nebula.frames");
  await sleep(secs * 1000);
  const out = JSON.parse(await E(`JSON.stringify({ stats: __nebula.stats(), frames: __nebula.frames - ${f0}, running: __nebula.running,
    viewport: [innerWidth, innerHeight], dpr: devicePixelRatio, readout: (document.querySelector('.nb-fps')||{}).textContent,
    gpu: (() => { const g = document.createElement('canvas').getContext('webgl'); const e = g && g.getExtension('WEBGL_debug_renderer_info'); return e ? g.getParameter(e.UNMASKED_RENDERER_WEBGL) : 'unknown'; })(),
    look: document.documentElement.dataset.look || null, clusters: __nebula.drawn.length, particles: __nebula.drawn.reduce((t, d) => t + d.particles, 0) })`));
  out.seconds = secs;
  out.url = url;
  console.log(JSON.stringify(out));
  await b.send("Browser.close").catch(() => {});
} finally {
  if (b) b.close();
  await sleep(500);
  spawnSync("pkill", ["-f", profile]);
  try { rmSync(profile, { recursive: true, force: true }); } catch { /* chrome may hold files briefly */ }
}
