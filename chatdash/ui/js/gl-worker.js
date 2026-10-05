// Runs the WebGL2 nebula off the main thread (OffscreenCanvas). The page posts the latest scene each frame; this
// worker draws only the newest one per animation frame, so a slow GPU never queues frames or delays typing.
import { GLNebula } from "./gl-nebula.js";

let g = null, pending = null, scheduled = false;
const tick = (fn) => (self.requestAnimationFrame ? self.requestAnimationFrame(fn) : setTimeout(fn, 16));
function draw() {
  scheduled = false;
  if (!g || !pending) return;
  const m = pending; pending = null;
  try { g.render(m.scene, m.t); } catch (err) { postMessage({ k: "error", msg: String((err && err.message) || err) }); }
}
onmessage = (e) => {
  const m = e.data;
  try {
    if (m.k === "init") { g = new GLNebula(m.canvas); postMessage({ k: "ready" }); }
    else if (m.k === "resize") { g.gasScale = m.gasScale; g.resize(m.w, m.h, m.dpr); }
    else if (m.k === "render") { pending = m; if (!scheduled) { scheduled = true; tick(draw); } }
  } catch (err) { postMessage({ k: "error", msg: String((err && err.message) || err) }); }
};
