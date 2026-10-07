"""Collect every chat across all Claude config dirs. Files only, zero model tokens.

Sources per config dir ($CFG = ~/.claude, ~/.claude-work, ...):
  $CFG/sessions/<pid>.json   live sessions (status busy / idle / waiting, kind bg / interactive)
  $CFG/jobs/<id>/state.json  background jobs (state, transcript path), incl. stopped ones
  $CFG/projects/*/*.jsonl    transcripts touched in the window (default 24 h)
A tool that hardlinks background records into every dir may leave a manifest (.cc-bridge.json) listing
them; those files are skipped to avoid duplicates (no manifest, nothing skipped).
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import threading
import time

from . import _plat, config
from .extract import Transcript, iso_epoch

HOME = os.path.expanduser("~")
WINDOW_S = 24 * 3600


def config_dirs() -> list[str]:
    """Connected accounts: every ~/.claude and ~/.claude-<name> with projects/ and sessions/, minus the ones
    disconnected (hidden) in Settings > Accounts."""
    from . import accounts
    hid = accounts.hidden()
    return [d for d in accounts.discovered() if accounts.account_name(d) not in hid]


def workstream_of(name: str | None) -> str | None:
    """The work item a chat name belongs to: the match of config work_item_pattern (none by default)."""
    return config.work_item_of(name)


def account_name(cfg: str) -> str:
    if cfg.startswith("provider:"):                 # the pseudo config dir of a Codex or Cursor row
        return cfg[len("provider:"):]
    b = os.path.basename(cfg)
    return "main" if b == ".claude" else b.replace(".claude-", "")


_PS: list = [0.0, set()]           # (taken at, running pids): one ps for every config dir in a snapshot


def _alive(pids: list[int]) -> set[int]:
    """Running (not stopped T / zombie Z) pids. One `ps -A` serves all 7 config dirs for a second: a ps per
    config dir was 85% of a snapshot (measured 2026-10-04: 7 forks, about 290 of 340 ms)."""
    if not pids:
        return set()
    if _plat.IS_WIN:
        return _plat.running_pids(pids)
    if time.time() - _PS[0] > 1.0:
        try:
            _PS[1] = _plat.all_running_pids()
        except (OSError, subprocess.TimeoutExpired):
            return set()
        _PS[0] = time.time()
    return set(pids) & _PS[1]


def live_sessions(cfg: str) -> list[dict]:
    sdir = os.path.join(cfg, "sessions")
    try:
        bridged = set(json.load(open(os.path.join(sdir, ".cc-bridge.json"), encoding="utf-8")).get("files", {}))
    except (OSError, ValueError):
        bridged = set()
    recs = []
    for f in glob.glob(os.path.join(sdir, "[0-9]*.json")):
        if os.path.basename(f) in bridged:
            continue
        try:
            d = json.load(open(f, encoding="utf-8"))
            d["pid"] = int(d.get("pid") or os.path.basename(f).split(".")[0])
        except (OSError, ValueError):
            continue
        recs.append(d)
    alive = _alive([r["pid"] for r in recs])
    return [r for r in recs if r["pid"] in alive]


def jobs(cfg: str, since: float) -> list[dict]:
    out = []
    for f in glob.glob(os.path.join(cfg, "jobs", "*", "state.json")):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        d["id"] = os.path.basename(os.path.dirname(f))
        upd = iso_epoch(d.get("updatedAt")) or os.path.getmtime(f)
        if d.get("state") in ("done", "stopped", "failed") and upd < since:
            continue
        d["_updated"] = upd
        out.append(d)
    return out


def transcripts(cfg: str, since: float) -> dict[str, str]:
    """sessionId -> transcript path, for top-level transcripts touched since `since`."""
    out = {}
    for p in glob.glob(os.path.join(cfg, "projects", "*", "*.jsonl")):
        try:
            if os.path.getmtime(p) >= since:
                out[os.path.basename(p)[:-6]] = p
        except OSError:
            continue
    return out


def find_transcript(cfg: str, sid: str) -> str | None:
    hits = glob.glob(os.path.join(cfg, "projects", "*", f"{sid}.jsonl"))
    return hits[0] if hits else None


class Collector:
    """Holds parsed transcripts across cycles; snapshot() returns the chat list."""

    def __init__(self, window_s: int = WINDOW_S):
        self.window_s = window_s
        self.tr: dict[str, Transcript] = {}
        self.lock = threading.Lock()
        self.seats: dict[str, dict] = {}
        self._meter_off = 0
        self._meter_last: dict[str, dict] = {}
        self._meter_hist: dict[str, list] = {}
        self._live_next: dict[str, float] = {}
        self._pr_cache: dict[str, tuple] = self._load_pr_cache()
        self._pr_busy = False
        self.provider_error = None

    def _transcript(self, path: str) -> Transcript:
        t = self.tr.get(path)
        if t is None:
            t = self.tr[path] = Transcript(path)
        t.update()
        return t

    def snapshot(self) -> list[dict]:
        since = time.time() - self.window_s
        chats: dict[str, dict] = {}
        today = time.strftime("%Y-%m-%d", time.gmtime())
        for cfg in config_dirs():
            acct = account_name(cfg)
            live = {r.get("sessionId"): r for r in live_sessions(cfg) if r.get("sessionId")}
            js = jobs(cfg, since)
            job_by_sid = {}
            for j in js:
                p = j.get("linkScanPath")
                sid = os.path.basename(p)[:-6] if p else j.get("sessionId")
                if sid:
                    job_by_sid[sid] = j
            paths = transcripts(cfg, since)
            for sid in set(live) | set(job_by_sid):
                if sid not in paths:
                    j = job_by_sid.get(sid) or {}
                    p = j.get("linkScanPath") if j.get("linkScanPath") and os.path.exists(j["linkScanPath"]) else find_transcript(cfg, sid)
                    if p:
                        paths[sid] = p
            for sid, path in paths.items():
                if "/subagents/" in path:
                    continue
                t = self._transcript(path)
                rec, job = live.get(sid), job_by_sid.get(sid)
                if rec is None and job is not None:
                    jsid = job.get("sessionId")
                    rec = live.get(jsid) if jsid else None
                chats[f"{acct}:{sid}"] = self._row(cfg, acct, sid, path, t, rec, job, today)
        try:                       # Codex and Cursor chats sit in the same list; a failure there never hides the Claude chats
            from . import providers
            for r in providers.rows(self.window_s):
                chats[r["key"]] = r
        except Exception as e:
            self.provider_error = f"{type(e).__name__}: {e}"[:200]
        return sorted(chats.values(), key=lambda c: c["activity"] or 0, reverse=True)

    def _row(self, cfg, acct, sid, path, t: Transcript, rec, job, today) -> dict:
        lt = t.last_turn()
        if job is not None and job.get("state") in ("stopped", "failed"):
            rec = None      # daemon pool pids outlive a stopped job; trust the job state
        if rec is not None:
            kind = "bg" if rec.get("kind") == "bg" else "interactive"
            st = rec.get("status")
            state = {"busy": "working", "waiting": "needs_you", "idle": "idle",
                     "shell": "working"}.get(st, st or "idle")
        elif job is not None:
            kind = "bg"
            js = job.get("state")
            # No live process. A job that finished keeps state "done" even after `claude stop`,
            # so without a live session record it is shown as stopped (attach still reopens it).
            state = "working" if js == "working" else "failed" if js == "failed" else "stopped"
            if js == "working" and (time.time() - (job.get("_updated") or 0)) > 600:
                state = "stopped"        # daemon record gone stale
        else:
            kind = "headless" if (t.entrypoint or "").startswith("sdk") else "closed"
            state = "stopped"
        name = ((rec or {}).get("name") or (job or {}).get("name") or t.label()
                or (lt["prompt"][:60] if lt["prompt"] else sid[:8]))
        idle = t.idle_minutes()
        # Prompt cache: 1 h TTL, refreshed by every API call. The last assistant record
        # is the last call, so its age says how warm the chat's cache still is.
        last_call = iso_epoch(t.last_ts)
        cache_age = (time.time() - last_call) / 60.0 if last_call else None
        if state == "working":
            warmth = "warm"
        elif cache_age is None:
            warmth = None
        else:
            warmth = "warm" if cache_age < 45 else "cooling" if cache_age < 60 else "cold"
        activity = max(filter(None, [iso_epoch(t.last_ts), iso_epoch(t.last_user_ts),
                                     os.path.getmtime(path) if os.path.exists(path) else None]),
                       default=None)
        return {
            "key": f"{acct}:{sid}", "account": acct, "config": cfg, "session_id": sid,
            "job_id": (job or {}).get("id") or (rec or {}).get("jobId"),
            "pid": (rec or {}).get("pid"), "live": rec is not None,
            "kind": kind, "state": state, "name": name,
            "waiting_for": ((job or {}).get("detail") or "waiting for you") if state == "needs_you" else None,
            "warmth": warmth, "cache_age_min": round(cache_age, 1) if cache_age is not None else None,
            "cools_in_min": round(60 - cache_age, 1) if cache_age is not None and cache_age < 60 else 0,
            "cwd": t.cwd or (rec or {}).get("cwd") or (job or {}).get("cwd"),
            "path": path, "model": t.model, "ttl": t.write_ttl,
            "last_prompt": lt["prompt"], "last_prompt_at": lt["pts"],
            "final": lt["final"], "final_at": lt["fts"],
            "turns": len(t.turns), "ctx_tokens": t.ctx_tokens,
            "idle_min": round(idle, 1) if idle is not None else None,
            "units_today": t.units_on(today), "cold_today": t.cold_recaches(today),
            "units_7d": round(sum(u[1] for u in t._usage.values()
                                  if u[0] >= time.strftime("%Y-%m-%d", time.gmtime(time.time() - 6 * 86400)))),
            "ws": workstream_of(name),
            "pending_tool": t.pending_tool if state in ("working", "needs_you") else None,
            "prs": list(t.prs.values())[-3:], "activity": activity, "version": t.version,
        }

    def turns(self, path: str) -> list[dict]:
        from .providers import board as _prow
        if _prow.is_path(path):
            from . import providers
            return providers.turns_for_path(path)
        return list(self._transcript(path).turns)

    # ------------------------------------------------------------ seats
    def _read_meter(self) -> None:
        """A status line you configure appends 'ts<TAB>config<TAB>session<TAB>{rate_limits}' to the meter log
        (config meter_log, default <data dir>/meter.log) whenever 5h/7d changes. Reading it costs no network
        call. Without the file there is no reading: limits show as unknown. Incremental, keeps 8 days."""
        meter_log = config.meter_log_path()
        try:
            st = os.stat(meter_log)
        except OSError:
            return
        if st.st_size < self._meter_off:
            self._meter_off = 0
        if st.st_size == self._meter_off:
            return
        with open(meter_log, "rb") as fh:
            fh.seek(self._meter_off)
            data = fh.read()
        self._meter_off += len(data)
        for raw in data.decode(errors="replace").splitlines():
            parts = raw.split("\t")
            cfg = next((p for p in parts if _plat.is_abs_path(p)), None)
            if not cfg or not parts[-1].startswith("{"):
                continue
            try:
                j = json.loads(parts[-1])
                t = iso_epoch(parts[0])
            except ValueError:
                continue
            fh5, s7 = j.get("five_hour") or {}, j.get("seven_day") or {}
            acct = account_name(cfg)
            rec = {"at": t, "five": fh5.get("used_percentage"), "seven": s7.get("used_percentage"),
                   "five_resets": fh5.get("resets_at"), "seven_resets": s7.get("resets_at")}
            self._meter_last[acct] = rec
            hist = self._meter_hist.setdefault(acct, [])
            hist.append((t, rec["five"], rec["seven"]))
        cut = time.time() - 8 * 86400
        for a in self._meter_hist:
            self._meter_hist[a] = [h for h in self._meter_hist[a] if h[0] and h[0] >= cut]

    def seat_usage(self) -> dict[str, dict]:
        """5h/7d per account: status-line log first; the live endpoint only for an account with no
        log entry in 30 min, at most once per 15 min, backing off 30 min after a failure."""
        self._read_meter()
        now = time.time()
        out = {}
        for cfg in config_dirs():
            acct = account_name(cfg)
            m = self._meter_last.get(acct)
            if (not m or now - (m["at"] or 0) > 1800) and now >= self._live_next.get(acct, 0):
                self._live_next[acct] = now + 900
                live = None
                ul = config.plugin("usage_live")      # optional: absent means no live reading, never a made-up one
                if ul is not None:
                    try:
                        live = ul.fetch_live(os.path.basename(cfg))
                    except Exception:
                        live = None
                if live:
                    m = self._meter_last[acct] = {"at": now, "five": live.get("five"),
                                                  "seven": live.get("seven"),
                                                  "five_resets": None, "seven_resets": None}
                    self._meter_hist.setdefault(acct, []).append((now, live.get("five"), live.get("seven")))
                else:
                    self._live_next[acct] = now + 1800
            m = m or {}
            out[acct] = {"account": acct, "config": cfg, "five": m.get("five"), "seven": m.get("seven"),
                         "five_resets": m.get("five_resets"), "seven_resets": m.get("seven_resets"),
                         "age_min": round((now - m["at"]) / 60) if m.get("at") else None,
                         "ok": m.get("five") is not None}
        self.seats = out
        return out

    def seat_history(self) -> dict[str, list]:
        self._read_meter()
        return {a: [[round(t), f, s] for t, f, s in h] for a, h in self._meter_hist.items()}

    def cost_days(self, days: int = 7) -> list[dict]:
        """Limit units per chat per UTC day for the last `days` days (from transcripts in window)."""
        day_list = [time.strftime("%Y-%m-%d", time.gmtime(time.time() - i * 86400)) for i in range(days)][::-1]
        rows = []
        for path, t in list(self.tr.items()):
            by = {d: 0.0 for d in day_list}
            for d, u, _w, _r in t._usage.values():
                if d in by:
                    by[d] += u
            if any(by.values()):
                rows.append({"path": path, "by_day": {d: round(v) for d, v in by.items()}})
        return rows

    # ------------------------------------------------------------ PR state
    # How long a PR answer is trusted before gh is asked again. merged and closed rarely change (a day); a PR gh says does
    # not exist (a placeholder link in a chat, a deleted repo) is "missing" and is not drawn; a failed or timed-out call
    # is retried soon and stays "unknown", which the graph words as "not read yet", never as a state.
    PR_TTL = {"open": 900, "merged": 86400, "closed": 86400, "missing": 6 * 3600, None: 120}

    @staticmethod
    def _pr_file() -> str:
        return os.path.join(config.home(), "pr_cache.json")

    def _load_pr_cache(self) -> dict[str, tuple]:
        """The last answers survive a restart: every PR used to read "unknown" for minutes after one."""
        try:
            return {u: (float(t), st) for u, (t, st) in json.load(open(self._pr_file(), encoding="utf-8")).items()}
        except (OSError, ValueError, TypeError):
            return {}

    def _save_pr_cache(self) -> None:
        try:
            os.makedirs(config.home(), exist_ok=True)
            tmp = self._pr_file() + ".tmp"
            json.dump({u: [t, st] for u, (t, st) in self._pr_cache.items()}, open(tmp, "w", encoding="utf-8"))
            os.replace(tmp, self._pr_file())
        except OSError:
            pass

    def pr_states(self, urls: list[str]) -> dict[str, str]:
        """open / merged / closed / missing per PR url via `gh`, fetched in a background thread (see PR_TTL)."""
        now = time.time()
        todo = [u for u in urls if u not in self._pr_cache or now - self._pr_cache[u][0] > self.PR_TTL.get(self._pr_cache[u][1], 900)]
        if todo and not self._pr_busy:
            self._pr_busy = True
            threading.Thread(target=self._fetch_prs, args=(todo,), daemon=True).start()
        return {u: self._pr_cache[u][1] for u in urls if u in self._pr_cache and self._pr_cache[u][1]}

    @staticmethod
    def _gh_state(u: str) -> str | None:
        try:
            p = subprocess.run(["gh", "pr", "view", u, "--json", "state", "-q", ".state"],
                               capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            return None
        out = p.stdout.strip().lower()
        if out in ("open", "merged", "closed"):
            return out
        if "could not resolve" in (p.stdout + p.stderr).lower():
            return "missing"                   # gh answered: no such repository or pull request
        return None                            # not an answer (auth, network, rate limit): try again soon

    def _fetch_prs(self, urls: list[str]) -> None:
        # six gh calls at a time: 116 PRs took about 2 minutes one by one, so every PR read "unknown" that long after a restart
        from concurrent.futures import ThreadPoolExecutor
        try:
            with ThreadPoolExecutor(max_workers=6) as pool:
                for i, (u, st) in enumerate(zip(urls, pool.map(self._gh_state, urls)), 1):
                    self._pr_cache[u] = (time.time(), st)
                    if i % 20 == 0:
                        self._save_pr_cache()
            self._save_pr_cache()
        finally:
            self._pr_busy = False
