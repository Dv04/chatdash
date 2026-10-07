"""Actions on chats, always through the unmodified `claude` binary under the chat's
own account (CLAUDE_CONFIG_DIR). Each reply costs one normal turn; nothing else here
touches a model.

Reply routing (measured 2026-09-25 on throwaway sessions, see DHI Orbit README):
  background session, live or stopped -> type into `claude attach <id>` through a pty.
      Same session, same transcript, warm cache (reply turn wrote 59 tokens, read 48,908).
      attach also reopens a stopped background session (one cold re-cache, then warm).
  live interactive terminal tab -> REFUSED. Typing into the tab works (AppleScript `do script ... in tab`,
      found by the process tty) but Terminal delivers it as a bracketed paste, so Return does not submit in
      Claude's input box: measured 2026-09-29 with `do script`, an empty `do script`, a CR, an ESC-closed
      paste, and TIOCSTI (blocked by macOS). Only a real keypress via System Events would (needs Accessibility).
      Resuming the session from a second process has no lock (claude-code #69364).
  closed chat with no background job -> `claude --resume <id> --bg "<text>"`.
      Note: measured, this makes a copy under a new session id and re-writes the whole
      context once (cold); later replies go through attach and are warm.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time

from . import _plat, config

CLAUDE = config.claude_bin()
PASTE_START, PASTE_END = b"\x1b[200~", b"\x1b[201~"


ATTACH_ROWS, ATTACH_COLS = 50, 220   # an unsized pty is 0x0 and the CLI then wraps at 120, splitting long
                                     # dialog labels across lines (measured 2026-10-05)


def _env(cfg: str) -> dict:
    env = dict(os.environ, TERM="xterm-256color", COLUMNS=str(ATTACH_COLS), LINES=str(ATTACH_ROWS))
    for v in ("CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_SESSION_ID",
              "CLAUDE_SESSION_ID", "CLAUDE_PID", "CLAUDE_CODE_ENTRYPOINT", "CLAUDECODE", "CLAUDE_JOB_DIR"):
        env.pop(v, None)
    if os.path.basename(cfg) == ".claude":
        env.pop("CLAUDE_CONFIG_DIR", None)      # the default dir must be unset, not set
    else:
        env["CLAUDE_CONFIG_DIR"] = cfg
    return env


def _drain(pty, seconds: float) -> bytes:
    end, buf = time.time() + seconds, b""
    while time.time() < end:
        chunk = pty.read(min(0.2, max(0.0, end - time.time())))
        if chunk is None:
            continue
        if not chunk:
            break
        buf += chunk
    return buf


def _drain_until(pty, ready, max_s: float, settle: float = 0.25) -> bytes:
    """Read until ready(buf) is true, then `settle` more seconds so the frame finishes painting; at most max_s.
    `claude attach` paints its input box in about 0.4 s (measured 2026-10-04: 0.29 to 0.44 s over 3 attaches), so a
    fixed multi-second drain only adds latency to every send."""
    end, buf, hit = time.time() + max_s, b"", None
    while time.time() < end and (hit is None or time.time() < hit):
        chunk = pty.read(0.05)
        if chunk is not None:
            if not chunk:
                break
            buf += chunk
        if hit is None and ready(buf):
            hit = min(end, time.time() + settle)
    return buf


def _input_ready(buf: bytes) -> bool:
    if _plat.IS_WIN:       # ConPTY repaints the screen and may not forward the paste-mode switch: the drawn box is the cue
        return "❯".encode() in buf
    return b"\x1b[?2004h" in buf and "❯".encode() in buf     # bracketed paste on and the prompt box drawn


def _transcript_has(path: str, text: str, since_size: int, timeout: float, pty=None,
                    screen: list | None = None) -> bool:
    """True once a user record containing `text` is appended after since_size; "queued" once the session's own
    queue took it instead (an `enqueue` record: the chat was mid-turn, and the CLI types it in when the turn
    ends, measured 2026-10-05: delivered 10 to 15 s later, after the attach client had closed). False on timeout.
    With `pty`, keeps reading the attach pty while waiting and appends what it read to `screen`."""
    needle = text.strip()[:60]
    import json
    end = time.time() + timeout
    while time.time() < end:
        try:
            if os.path.getsize(path) > since_size:
                with open(path, "rb") as fh:
                    fh.seek(since_size)
                    queued = False
                    for raw in fh.read().split(b"\n"):
                        if b'"queue-operation"' in raw and b'"enqueue"' in raw:
                            try:
                                r = json.loads(raw)
                            except ValueError:
                                continue
                            if r.get("operation") == "enqueue" and needle and needle in str(r.get("content") or ""):
                                queued = True
                            continue
                        if b'"type":"user"' not in raw and b'"type": "user"' not in raw:
                            continue
                        try:
                            r = json.loads(raw)
                        except ValueError:
                            continue
                        c = (r.get("message") or {}).get("content")
                        s = c if isinstance(c, str) else " ".join(
                            b.get("text", "") for b in (c or []) if isinstance(b, dict))
                        if needle and needle in s:
                            return True
                    if queued:
                        return "queued"
        except OSError:
            pass
        if pty is None:
            time.sleep(0.15)
        else:
            out = _drain(pty, 0.15)
            if not out:
                time.sleep(0.05)              # pty closed (attach exited): avoid a busy loop
            elif screen is not None:
                screen.append(out)
    return False


def _spawn_attach(cfg: str, job_id: str, cwd: str | None):
    """Run `claude attach <job>` on a fresh pty (see _plat.Pty: openpty + Popen on POSIX, ConPTY on Windows)."""
    d = cwd if cwd and os.path.isdir(cwd) else config.default_cwd()
    if not os.path.isdir(d):
        d = os.path.expanduser("~")
    return _plat.Pty([CLAUDE, "attach", job_id], _env(cfg), d, ATTACH_ROWS, ATTACH_COLS)


def _close_attach(pty) -> None:
    """Close the pty and end the attach client; the background session itself keeps running."""
    _plat.stop_pty(pty)


def type_into_attach(cfg: str, job_id: str, text: str, cwd: str | None,
                     transcript: str | None, confirm_s: float = 25) -> dict:
    """Open `claude attach <job>` in a pty, paste the text, press Enter, confirm it landed
    in the transcript, then close the pty (the background session keeps running)."""
    size0 = os.path.getsize(transcript) if transcript and os.path.exists(transcript) else 0
    pty = _spawn_attach(cfg, job_id, cwd)
    screen = b""
    try:
        screen = _drain_until(pty, _input_ready, 6)
        pty.write(PASTE_START + text.encode() + PASTE_END)
        later: list = []
        later.append(_drain(pty, 0.4))
        pty.write(b"\r")
        landed = _transcript_has(transcript, text, size0, confirm_s, pty, later) if transcript else None
        _drain(pty, 0.2)
        screen += b"".join(later)
    except OSError as e:                     # attach already exited (job gone or stopped): the pty is closed
        return {"ok": False, "route": "attach", "error": f"claude attach {job_id} exited before the prompt was "
                f"typed ({e.strerror}); the chat may be gone", "screen": screen[-400:].decode(errors="replace")}
    finally:
        _close_attach(pty)
    if landed is False:
        # Seen 2026-10-05 (2 of about 60 sends, not reproduced since): Enter pressed in the last half second of the
        # previous turn was neither submitted nor queued, and the message landed the moment the attach client
        # closed. Look once more after closing, so a delivered message is not reported as failed (and resent).
        landed = _transcript_has(transcript, text, size0, 3.0)
        if landed:
            return {"ok": True, "route": "attach", "queued": landed == "queued", "confirmed": landed is True,
                    "late": True}
        tail = screen[-400:].decode(errors="replace")
        return {"ok": False, "route": "attach", "error": "prompt did not reach the transcript "
                "within %ds; the session may be busy or showing a dialog" % confirm_s, "screen": tail}
    if landed == "queued":
        return {"ok": True, "route": "attach", "queued": True, "confirmed": False}
    return {"ok": True, "route": "attach", "confirmed": bool(landed)}


def _strip_ansi(b: bytes) -> str:
    import re
    s = b.decode(errors="replace")
    s = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", " ", s)
    s = re.sub(r"\x1b\][^\x07]*\x07", " ", s)
    return re.sub(r"[ \t]+", " ", s)


OPT_RE = None


def parse_dialog(screen: str) -> dict | None:
    """The dialog at the bottom of a session's screen, whatever kind it is:
      permission  "Do you want to ...?"  1. Yes  2. Yes, and ...  3. No   (Esc to cancel)
      question    AskUserQuestion: optional tab bar "← ☐ A ☐ B ✔ Submit →", question text,
                  numbered options with description lines, "Type something.", "Chat about this",
                  footer "Enter to select · Tab/Arrow keys to navigate · Esc to cancel"
    Returns {kind, question, tabs, options:[{n,label,desc,free}], cursor} or None."""
    import re
    global OPT_RE
    OPT_RE = OPT_RE or re.compile(r"^(❯\s*)?(\d{1,2})\.\s+(\S.*)$")
    text = re.sub(r"[─-╿]+", " ", screen)            # box drawing
    lines = [" ".join(l.split()) for l in re.split(r"\r\n|\r|\n", text)]
    end = None
    for i in range(len(lines) - 1, max(-1, len(lines) - 400), -1):
        if "Enter to select" in lines[i] or "Esc to cancel" in lines[i]:
            end = i
            break
    if end is None:
        return None
    # the options block: walk up from the footer while lines are options or their descriptions
    opts, first = [], None
    for i in range(end - 1, max(-1, end - 80), -1):
        m = OPT_RE.match(lines[i])
        if m:
            first = i
            opts.insert(0, {"n": int(m.group(2)), "label": m.group(3).strip(), "desc": "",
                            "cursor": bool(m.group(1))})
        elif lines[i].startswith(("Do you want", "←")) or (first is not None and i < first - 3):
            break
    if not opts:
        return None
    # descriptions: non-option lines between options
    cur = None
    for i in range(first, end):
        m = OPT_RE.match(lines[i])
        if m:
            cur = next(o for o in opts if o["n"] == int(m.group(2)))
        elif lines[i] and cur is not None and "Enter to select" not in lines[i]:
            cur["desc"] = (cur["desc"] + " " + lines[i]).strip()[:300]
    head = [l for l in lines[max(0, first - 12):first] if l]
    kind, tabs, question = "question", None, ""
    for j in range(len(head) - 1, -1, -1):
        if head[j].startswith("Do you want"):
            kind, question = "permission", head[j]
            break
        if head[j].startswith("←"):
            tabs, question = head[j], " ".join(head[j + 1:])
            break
    if not question:
        # no tab bar (a single question): the screen above it is the echoed prompt ("❯ ...") and the "☐ Header" chip,
        # neither is the question
        k = max((j for j, l in enumerate(head) if l.startswith(("❯", "☐"))), default=-1)
        question = " ".join(head[k + 1:][-3:]) or " ".join(head[-3:])
    for o in opts:
        o["free"] = o["label"].lower().startswith("type something")
    cursor = next((o["n"] for o in opts if o["cursor"]), opts[0]["n"])
    return {"kind": kind, "question": question[:600], "tabs": tabs, "options": opts, "cursor": cursor}


def read_dialog(chat: dict) -> dict | None:
    """Read the current dialog of a background session from `claude logs` (read-only)."""
    if not chat.get("job_id"):
        return None
    try:
        p = subprocess.run([CLAUDE, "logs", chat["job_id"]], env=_env(chat["config"]),
                           capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_dialog(_strip_ansi(p.stdout[-60000:]))


def answer_dialog(chat: dict, n: int, label: str = "", text: str = "") -> dict:
    """Pick option n in the session's dialog (0 = Esc / cancel). Attaches in a pty, re-reads the
    dialog, refuses if it is not the one the page showed (label mismatch), moves the cursor with
    arrow keys, then Enter (or types `text` into a 'Type something' option). Never guesses."""
    if not chat.get("job_id"):
        return {"ok": False, "error": "only background sessions can be answered here; use Terminal"}
    pty = _spawn_attach(chat["config"], chat["job_id"], chat.get("cwd"))
    try:
        dlg = parse_dialog(_strip_ansi(_drain_until(pty, lambda b: parse_dialog(_strip_ansi(b[-60000:])) is not None, 6, 0.3)))
        if not dlg:
            return {"ok": False, "error": "no dialog on screen right now (already answered, or the chat "
                                          "is waiting on something else); refresh"}
        if n == 0:
            pty.write(b"\x1b")
            sent = "Esc"
        else:
            opt = next((o for o in dlg["options"] if o["n"] == n), None)
            if not opt:
                return {"ok": False, "error": f"option {n} is not in the dialog now", "dialog": dlg}
            if label and not opt["label"].startswith(label[:20]):
                return {"ok": False, "error": "the dialog changed since the page showed it; refresh",
                        "dialog": dlg}
            steps = n - dlg["cursor"]
            for _ in range(abs(steps)):
                pty.write(b"\x1b[B" if steps > 0 else b"\x1b[A")
                time.sleep(0.12)
            if opt["free"] and text:
                time.sleep(0.2)
                pty.write(PASTE_START + text.encode() + PASTE_END)
                time.sleep(0.4)
            pty.write(b"\r")
            sent = f"{n}. {opt['label'][:40]}" + (" + text" if opt["free"] and text else "")
        def moved(b: bytes) -> bool:            # the dialog closed or changed: no need to wait the full 3 s
            a = parse_dialog(_strip_ansi(b[-60000:]))
            return a is None or (a["question"], a["tabs"], a["cursor"]) != (dlg["question"], dlg["tabs"], dlg["cursor"])
        after = parse_dialog(_strip_ansi(_drain_until(pty, moved, 3, 0.4)))
        same = bool(after and after["question"] == dlg["question"] and after["tabs"] == dlg["tabs"]
                    and after["cursor"] == dlg["cursor"])
        return {"ok": not same, "sent": sent, "next": after,
                "error": "the dialog did not change; open Terminal to check" if same else None}
    finally:
        _close_attach(pty)


def resume_bg(cfg: str, session_id: str, text: str, cwd: str | None) -> dict:
    p = subprocess.run([CLAUDE, "--resume", session_id, "--bg", text], cwd=cwd or config.default_cwd(),
                       env=_env(cfg), capture_output=True, text=True, timeout=60)
    import re
    m = re.search(r"attach ([0-9a-f]{8})", p.stdout + p.stderr)
    if p.returncode != 0 or not m:
        return {"ok": False, "route": "resume-bg", "error": (p.stderr or p.stdout)[-400:]}
    return {"ok": True, "route": "resume-bg", "new_job": m.group(1),
            "note": "resumed as a background copy (new id); the first reply re-caches the chat once"}


def reply(chat: dict, text: str, manual: bool = False) -> dict:
    """Send text to a chat. `manual` is True only for a message the user typed on the board: automations (limit resume, idle
    compaction, keep-warm, handoff, bulk compact) call this without it and are refused for Codex and Cursor chats, which they
    were never built for (they drive the `claude` binary by session id)."""
    text = text.strip()
    if not text:
        return {"ok": False, "error": "empty message"}
    if chat.get("provider"):
        if not manual:
            return {"ok": False, "route": "refused", "error": f"Automations do not act on {chat.get('provider_label') or chat['provider']} chats."}
        from . import providers
        return providers.reply(chat["provider"], chat["session_id"], text)
    if chat["kind"] == "interactive" and chat["live"]:
        return {"ok": False, "route": "refused",
                "error": "This chat is open in a terminal tab. Text can be typed into the tab from here, but "
                         "Claude's input box does not treat the Return as Enter, so it would sit unsent in your "
                         "prompt. Type there, or close the tab and reply from here."}
    if chat["state"] == "working":
        return {"ok": False, "route": "refused",
                "error": "The chat is working right now. Reply when it is idle (or queue it)."}
    if chat.get("job_id"):
        return type_into_attach(chat["config"], chat["job_id"], text, chat.get("cwd"), chat.get("path"))
    return resume_bg(chat["config"], chat["session_id"], text, chat.get("cwd"))


def new_chat(cfg: str, name: str, text: str, cwd: str) -> dict:
    p = subprocess.run([CLAUDE, "--bg", "-n", name, text], cwd=cwd, env=_env(cfg),
                       capture_output=True, text=True, timeout=60)
    import re
    m = re.search(r"attach ([0-9a-f]{8})", p.stdout + p.stderr)
    if not m:
        return {"ok": False, "error": (p.stderr or p.stdout)[-400:]}
    return {"ok": True, "job_id": m.group(1)}


def stop(chat: dict) -> dict:
    if not chat.get("job_id"):
        return {"ok": False, "error": "not a background session"}
    p = subprocess.run([CLAUDE, "stop", chat["job_id"]], env=_env(chat["config"]),
                       capture_output=True, text=True, timeout=45)
    return {"ok": p.returncode == 0, "out": (p.stdout + p.stderr)[-200:]}


def open_terminal(chat: dict) -> dict:
    """Open a terminal window attached to (or resuming) the chat: macOS Terminal, or a new console window on Windows."""
    cwd = chat.get("cwd") or config.default_cwd()
    if chat.get("provider"):
        named = False
        tail = {"codex": f"codex resume {chat['session_id']}", "cursor": f"cursor-agent --resume {chat['session_id']}"}.get(
            chat["provider"], "")
        if not tail:
            return {"ok": False, "error": "no terminal command for this chat"}
    else:
        named = os.path.basename(chat["config"]) != ".claude"
        tail = f"claude attach {chat['job_id']}" if chat.get("job_id") else f"claude --resume {chat['session_id']}"
    if _plat.IS_WIN:
        cmd = f'cd /d "{cwd}" && ' + (f'set "CLAUDE_CONFIG_DIR={chat["config"]}" && ' if named else "") + tail
        try:
            subprocess.Popen(["cmd.exe", "/k", cmd], creationflags=subprocess.CREATE_NEW_CONSOLE)
        except OSError as e:
            return {"ok": False, "cmd": cmd, "error": f"could not open a console: {e.strerror}. Run: {cmd}"}
        return {"ok": True, "cmd": cmd, "error": None}
    envp = f"CLAUDE_CONFIG_DIR={shlex.quote(chat['config'])} " if named else ""
    cmd = f"cd {shlex.quote(cwd)} && " + envp + tail
    if sys.platform != "darwin":
        return {"ok": False, "cmd": cmd, "error": f"Opening a terminal from the page works on macOS and Windows only. Run: {cmd}"}
    script = f'tell application "Terminal" to do script {json_str(cmd)}\ntell application "Terminal" to activate'
    p = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=15)
    return {"ok": p.returncode == 0, "cmd": cmd, "error": p.stderr[-200:] if p.returncode else None}


def json_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def notify(title: str, body: str) -> None:
    """A desktop notification: macOS Notification Center, a Windows tray balloon, or notify-send on Linux;
    silently nothing elsewhere."""
    import shutil
    kw: dict = {}
    if sys.platform == "darwin":
        script = f"display notification {json_str(body[:180])} with title {json_str(title[:60])}"
        cmd = ["osascript", "-e", script]
    elif _plat.IS_WIN:
        q = lambda t: t.replace("'", "''").replace("\r", " ").replace("\n", " ")
        ps = ("Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
              "$n=New-Object System.Windows.Forms.NotifyIcon;$n.Icon=[System.Drawing.SystemIcons]::Information;"
              f"$n.Visible=$true;$n.ShowBalloonTip(8000,'{q(title[:60])}','{q(body[:180])}',"
              "[System.Windows.Forms.ToolTipIcon]::None);Start-Sleep -Seconds 9;$n.Dispose()")
        cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", ps]
        kw["creationflags"] = 0x08000000                       # CREATE_NO_WINDOW
    elif shutil.which("notify-send"):
        cmd = ["notify-send", title[:60], body[:180]]
    else:
        return
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
    except OSError:
        pass
