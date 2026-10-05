#!/usr/bin/env node
// Screenshot + network log through the Chrome DevTools protocol, with real device emulation.
// (Plain `--headless --window-size=390,...` lays the page out at 500 px and crops: measured 2026-10-02.)
//
//   node shoot.mjs specs.json      specs: [{url, out, width, height, dark?, mobile?, wait?, eval?, focus?, preScript?, reducedMotion?}]
// focus: emulate a focused page (headless targets are unfocused, which freezes the nebula field by design).
// preScript: runs before any page script on every load (e.g. a requestAnimationFrame counter the page cannot skip).
// Prints one JSON line per spec: {out, width, innerWidth, scrollWidth, requests:[...], external:[...], errors:[...]}
import { spawn } from "node:child_process";
import { mkdtempSync, readFileSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const CHROME = process.env.CHROME || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const specs = JSON.parse(readFileSync(process.argv[2], "utf8"));
const profile = mkdtempSync(join(tmpdir(), "cp2-shoot-"));
const port = 9300 + Math.floor(Math.random() * 500);
// SHOOT_GL=1: software WebGL2 (SwiftShader) so the nebula's GL path renders headless; correctness only, never a perf number.
const GLFLAGS = process.env.SHOOT_GL === "1" ? ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"] : ["--disable-gpu"];
const chrome = spawn(CHROME, ["--headless=new", ...GLFLAGS, "--hide-scrollbars", "--no-first-run",
  `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`, "about:blank"], { stdio: "ignore" });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function version() {
  for (let i = 0; i < 60; i++) {
    try { return await (await fetch(`http://127.0.0.1:${port}/json/version`)).json(); } catch { await sleep(150); }
  }
  throw new Error("chrome did not start");
}

function session(wsUrl) {
  const ws = new WebSocket(wsUrl);
  let id = 0;
  const waiting = new Map();
  const listeners = [];
  ws.onmessage = (m) => {
    const msg = JSON.parse(m.data);
    if (msg.id && waiting.has(msg.id)) { const { ok, no } = waiting.get(msg.id); waiting.delete(msg.id); msg.error ? no(new Error(msg.error.message)) : ok(msg.result); }
    else listeners.forEach((f) => f(msg));
  };
  return new Promise((res) => ws.onopen = () => res({
    send: (method, params = {}, sessionId) => new Promise((ok, no) => { const i = ++id; waiting.set(i, { ok, no }); ws.send(JSON.stringify({ id: i, method, params, sessionId })); }),
    on: (f) => listeners.push(f),
    close: () => ws.close(),
  }));
}

try {
  const v = await version();
  const b = await session(v.webSocketDebuggerUrl);
  for (const s of specs) {
    const { targetId } = await b.send("Target.createTarget", { url: "about:blank" });
    const { sessionId } = await b.send("Target.attachToTarget", { targetId, flatten: true });
    const S = (m, p) => b.send(m, p, sessionId);
    const requests = [], errors = [], timing = new Map();
    b.on((msg) => {
      if (msg.sessionId !== sessionId) return;
      if (msg.method === "Network.requestWillBeSent") { requests.push(msg.params.request.url); timing.set(msg.params.requestId, { url: msg.params.request.url, t0: msg.params.timestamp }); }
      if (msg.method === "Network.loadingFinished" && timing.has(msg.params.requestId)) timing.get(msg.params.requestId).t1 = msg.params.timestamp;
      if (msg.method === "Network.loadingFailed" && timing.has(msg.params.requestId)) timing.get(msg.params.requestId).failed = msg.params.errorText;
      if (msg.method === "Runtime.exceptionThrown") errors.push(msg.params.exceptionDetails.exception?.description || msg.params.exceptionDetails.text);
      if (msg.method === "Runtime.consoleAPICalled" && msg.params.type === "error") errors.push(msg.params.args.map((a) => a.value || a.description).join(" "));
    });
    await S("Network.enable"); await S("Runtime.enable"); await S("Page.enable");
    await S("Emulation.setDeviceMetricsOverride", { width: s.width, height: s.height, deviceScaleFactor: s.scale || 1, mobile: !!s.mobile });
    if (s.mobile) await S("Emulation.setTouchEmulationEnabled", { enabled: true });
    await S("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: s.dark ? "dark" : "light" },
      { name: "prefers-reduced-motion", value: s.reducedMotion ? "reduce" : "no-preference" }] });
    if (s.focus) await S("Emulation.setFocusEmulationEnabled", { enabled: true });
    if (s.preScript) await S("Page.addScriptToEvaluateOnNewDocument", { source: s.preScript });
    await S("Page.navigate", { url: s.url });
    await sleep(s.wait || 2500);
    if (s.offlineReload) {          // B11: does the cached shell open with the network gone?
      await S("Network.emulateNetworkConditions", { offline: true, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
      await S("Page.reload", { ignoreCache: false });
      await sleep(s.offlineWait || 3000);
    }
    let evalResult;
    if (s.eval) { evalResult = (await S("Runtime.evaluate", { expression: s.eval, awaitPromise: true, returnByValue: true })).result.value; await sleep(s.after || 600); }
    const dims = (await S("Runtime.evaluate", { expression: "JSON.stringify({w: innerWidth, sw: document.documentElement.scrollWidth, h: document.documentElement.scrollHeight})", returnByValue: true })).result.value;
    const d = JSON.parse(dims);
    const full = s.full ? { captureBeyondViewport: true, clip: { x: 0, y: 0, width: s.width, height: Math.min(d.h, 6000), scale: 1 } } : {};
    const shot = await S("Page.captureScreenshot", { format: "png", ...full });
    writeFileSync(s.out, Buffer.from(shot.data, "base64"));
    const origin = new URL(s.url).origin;
    const external = requests.filter((u) => !u.startsWith(origin) && !u.startsWith("data:") && !u.startsWith("about:") && !u.startsWith("blob:"));
    const slow = [...timing.values()].map((r) => ({ url: r.url.replace(origin, ""), ms: r.t1 ? Math.round((r.t1 - r.t0) * 1000) : null, failed: r.failed }))
      .filter((r) => r.ms == null || r.ms > 300 || r.failed);
    console.log(JSON.stringify({ slow, out: s.out, width: s.width, innerWidth: d.w, scrollWidth: d.sw, height: d.h, requests: requests.length, external, errors, eval: evalResult }));
    await b.send("Target.closeTarget", { targetId });
  }
  b.close();
} finally {
  chrome.kill("SIGKILL");
  try { rmSync(profile, { recursive: true, force: true }); } catch { /* chrome may hold files briefly */ }
}
