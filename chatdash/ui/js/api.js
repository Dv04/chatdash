// Contract v0 client. Same origin, /api/cp/, X-Token from the page (injected by the server).
export const TOKEN = window.CP_TOKEN && window.CP_TOKEN !== "__TOKEN__" ? window.CP_TOKEN : "";

export async function call(method, path, body) {
  const res = await fetch("/api/cp/" + path, {
    method,
    headers: { "X-Token": TOKEN, ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
    cache: "no-store",
  });
  let data = null;
  try { data = await res.json(); } catch { /* empty */ }
  if (!res.ok) {
    const err = new Error((data && data.error) || `HTTP ${res.status}`);
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}
export const get = (p) => call("GET", p);
export const post = (p, b) => call("POST", p, b || {});
export const put = (p, b) => call("PUT", p, b || {});

// Poller: keeps the last good value and how stale it is; a failure never shows old data as fresh.
export class Feed {
  constructor(path, everyMs, onData) {
    this.path = path; this.every = everyMs; this.onData = onData;
    this.data = null; this.okAt = 0; this.error = null; this.timer = null;
  }
  async tick() {
    try {
      this.data = await get(this.path);
      this.okAt = Date.now();
      this.error = null;
    } catch (e) {
      this.error = e;
    }
    this.onData(this);
  }
  start() { this.loop(); }
  // One loop per feed: a now() while a fetch is in flight asks for one more fetch after it instead of starting a
  // second loop (two loops would double the polling for good).
  async loop() {
    if (this.busy) { this.again = true; return; }
    clearTimeout(this.timer);
    this.busy = true;
    try { await this.tick(); } finally { this.busy = false; }
    if (this.again) { this.again = false; this.loop(); return; }
    this.timer = setTimeout(() => this.loop(), document.hidden ? this.every * 3 : this.every);
  }
  now() { this.loop(); }
  staleSec() { return this.okAt ? (Date.now() - this.okAt) / 1000 : Infinity; }
}
