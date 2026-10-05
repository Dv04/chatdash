"""Intent routing for the command palette (B7) and conversation mode (B9).

One closed command set. A deterministic parser handles the typed/spoken forms; only when it finds nothing
does the local model (ollama qwen3.5:2b, native /api/chat, think false, JSON format) map the text onto the
SAME closed set, and its answer is validated against real names before it is returned. Nothing here acts:
the client shows the parsed command and the user confirms; destructive commands always carry confirm=True.
"""
from __future__ import annotations

import json
import re
import urllib.request
from difflib import SequenceMatcher

OLLAMA = "http://127.0.0.1:11434/api/chat"
MODEL = "qwen3.5:2b-q4_K_M"
COMMANDS = {
    "needs_you": "read what needs the user",
    "status": "overall status and capacity",
    "answer": "answer decision N with option K",
    "reply": "send TEXT to CHAT",
    "stop": "stop CHAT",
    "spawn": "start a session on WORK_ITEM (optionally on SEAT)",
    "open": "open CHAT or WORK_ITEM",
    "graph": "show the graph",
    "board": "show the board",
    "next": "next decision",
    "skip": "skip this decision",
}
DESTRUCTIVE = {"stop", "reply", "spawn", "answer"}
NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "first": 1, "second": 2, "third": 3, "fourth": 4}


def best(name: str, names: list[str], floor: float = 0.45) -> str | None:
    """Exact beats prefix beats word-start beats substring beats fuzzy; ties go to the shortest name
    ("alpha" must not pick "tryalpha"; "PROJ-10" must pick the work item, not a chat named "PROJ-10 ...")."""
    name = name.lower().strip()
    if not name or not names:
        return None
    def score(n):
        nl = n.lower()
        if nl == name:
            return 4.0
        if nl.startswith(name):
            return 3.0
        if re.search(r"\b" + re.escape(name), nl):
            return 2.0
        if name in nl:
            return 1.5
        return SequenceMatcher(None, name, nl).ratio()
    s, _, n = max((score(n), -len(n), n) for n in names)
    return n if s >= floor else None


def num(tok: str) -> int | None:
    tok = tok.lower().strip(" .")
    return int(tok) if tok.isdigit() else NUM.get(tok)


def parse(text: str, ctx: dict) -> dict | None:
    """Deterministic grammar. ctx: {chats: [names], work_items: [ids or titles], seats: [names]}."""
    t = " ".join((text or "").strip().split())
    low = t.lower().rstrip("?.!")
    if not t:
        return None
    if low in ("what needs me", "what needs me now", "needs you", "what's waiting", "whats waiting", "briefing", "read it"):
        return {"cmd": "needs_you"}
    if low in ("status", "how are the seats", "capacity", "seats"):
        return {"cmd": "status"}
    if low in ("graph", "show graph", "open graph", "show the graph"):
        return {"cmd": "graph"}
    if low in ("board", "show board", "back", "show the board"):
        return {"cmd": "board"}
    if low in ("next", "next one", "next decision"):
        return {"cmd": "next"}
    if low in ("skip", "skip it", "later"):
        return {"cmd": "skip"}
    m = re.match(r"^(?:answer|pick|choose|option)\s+(?:decision\s+)?(\w+)?\s*(?:with\s+|option\s+)?(\w+)$", low)
    if m and num(m.group(2) or ""):
        return {"cmd": "answer", "decision": num(m.group(1)) if m.group(1) and num(m.group(1)) else None,
                "option": num(m.group(2)), "confirm": True}
    m = re.match(r"^(?:option|number)\s+(\w+)$", low)
    if m and num(m.group(1)):
        return {"cmd": "answer", "decision": None, "option": num(m.group(1)), "confirm": True}
    m = re.match(r"^(?:reply|send|tell)\s+(.+?)\s+to\s+(.+)$", t, re.I)
    if m:
        chat = best(m.group(2), ctx.get("chats", []))
        if chat:
            return {"cmd": "reply", "text": m.group(1).strip(" \"'"), "chat": chat, "confirm": True}
    m = re.match(r"^(?:tell|ask)\s+(.+?)\s+(?:to\s+)?(.+)$", t, re.I)
    if m and best(m.group(1), ctx.get("chats", []), 0.6):
        return {"cmd": "reply", "text": m.group(2).strip(" \"'"), "chat": best(m.group(1), ctx["chats"], 0.6), "confirm": True}
    m = re.match(r"^stop\s+(.+)$", t, re.I)
    if m:
        chat = best(m.group(1), ctx.get("chats", []))
        if chat:
            return {"cmd": "stop", "chat": chat, "confirm": True}
    m = re.match(r"^(?:spawn|start|new session)\s+(?:a\s+session\s+)?(?:on|for)?\s*(.+?)(?:\s+on\s+(\w+))?$", t, re.I)
    if m:
        wi = best(m.group(1), ctx.get("work_items", []))
        seat = best(m.group(2), ctx.get("seats", []), 0.8) if m.group(2) else None
        if wi:
            return {"cmd": "spawn", "work_item": wi, "seat": seat, "confirm": True}
    m = re.match(r"^(?:open|go to|show)\s+(.+)$", t, re.I)
    if m:
        target = best(m.group(1), ctx.get("work_items", []) + ctx.get("chats", []))
        if target:
            return {"cmd": "open", "target": target}
    return None


def route(text: str, ctx: dict, use_model: bool = True, post=None) -> dict:
    """-> {cmd..., source: 'grammar'|'model'|'none'}. Model output is validated against ctx names."""
    p = parse(text, ctx)
    if p:
        return {**p, "source": "grammar"}
    if not use_model:
        return {"cmd": None, "source": "none"}
    prompt = ("Map the operator's words to ONE command, or null if none clearly fits. Examples:\n"
              '"anything for me?" -> {"cmd": "needs_you"}\n"how full are the accounts" -> {"cmd": "status"}\n'
              '"take the first option" -> {"cmd": "answer", "option": 1}\n"not now" -> {"cmd": "skip"}\n'
              '"tell the papers chat to stop" -> {"cmd": "reply", "chat": "<papers chat>", "text": "stop"}\n'
              '"what is the weather" -> {"cmd": null}\nCommands: ' + json.dumps(COMMANDS) +
              ". Known chats: " + json.dumps(ctx.get("chats", [])[:40]) + ". Work items: " + json.dumps(ctx.get("work_items", [])[:20]) +
              ". Seats: " + json.dumps(ctx.get("seats", [])) + '. Output JSON only: {"cmd": <command or null>, "chat": <known chat or null>, '
              '"work_item": <known work item or null>, "seat": <seat or null>, "text": <text to send or null>, "decision": <n or null>, '
              '"option": <n or null>}. If unsure, cmd null.\nWords: ' + text)
    body = json.dumps({"model": MODEL, "stream": False, "think": False, "format": "json",
                       "options": {"temperature": 0}, "messages": [{"role": "user", "content": prompt}]}).encode()
    try:
        if post:
            raw = post(body)
        else:
            req = urllib.request.Request(OLLAMA, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
        out = json.loads(json.loads(raw)["message"]["content"])
    except Exception as e:
        return {"cmd": None, "source": "model", "error": f"{type(e).__name__}: {e}"[:200]}
    cmd = out.get("cmd")
    if cmd not in COMMANDS:
        return {"cmd": None, "source": "model"}
    res = {"cmd": cmd, "source": "model"}
    for k, pool in (("chat", "chats"), ("work_item", "work_items"), ("seat", "seats")):
        if out.get(k):
            v = best(str(out[k]), ctx.get(pool, []), 0.6)
            if v is None:
                return {"cmd": None, "source": "model", "error": f"unknown {k} {out[k]!r}"}
            res[k] = v
    for k in ("text", "decision", "option"):
        if out.get(k) is not None:
            res[k] = out[k]
    if cmd == "reply" and not (res.get("chat") and res.get("text")):
        return {"cmd": None, "source": "model", "error": "reply needs a chat and text"}
    # Held-out test 2026-10-02: the model routed 6 of 9 right (plus a correct null) and once invented a spawn,
    # so EVERY model-routed command is read back and needs the user's yes, destructive or not.
    res["confirm"] = True
    return res


def context_from(snap: dict, wis: list[dict]) -> tuple[dict, dict]:
    """(names for the router, reverse map name -> {session_id, key} / work item id) from the live snapshot."""
    chats = [c for c in snap["chats"] if c["kind"] in ("bg", "interactive") and not c.get("excluded")]
    names, rev = [], {}
    for c in chats:
        names.append(c["name"])
        rev[c["name"]] = {"session_id": c["session_id"], "key": c["key"], "title": c["name"]}
    w_names = []
    for w in wis:
        w_names.append(w["id"])
        rev[w["id"]] = {"work_item": w["id"], "title": w.get("title")}
    seats = [s["seat"] for s in snap["seats"] if not s.get("excluded")]
    return {"chats": names, "work_items": w_names, "seats": seats}, rev
