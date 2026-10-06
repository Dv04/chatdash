"""Risk of a question for the user, by deterministic rules (a design choice): keyword rules set the
floor; the chat may raise the level with "risk: med" / "risk: high" in the header or question, never
lower it. Low is the default only when no rule matches."""
from __future__ import annotations

import re

HIGH = re.compile(r"\b(deploy\w*|send\w*|e-?mail\w*|delete\w*|remov\w+|rm -rf|drop|merg\w+|push\w*|force|payment\w*|"
                  r"pay|invoice|stripe|refund|live|production|prod|publish\w*|post\w*|tweet|outreach|customer|"
                  r"wrangler|d1|migrat\w+|restart\w*|reboot\w*|shutdown|ssh|credential\w*|secret\w*|token|key|"
                  r"billing|charge|purchase|buy|cancel\w*|revok\w+|rotate)\b", re.I)
MED = re.compile(r"\b(install\w*|commit\w*|pr|pull request|branch|rewrite|overwrite|refactor\w*|rename|move|"
                 r"config\w*|settings?|schedul\w+|cron|spend|cost|quota|seat|upgrade|downgrade|dependency|"
                 r"cleanup|clean up|archive)\b", re.I)
RAISE = re.compile(r"\brisk\s*[:=]\s*(low|med|medium|high)\b", re.I)
ORDER = {"low": 0, "med": 1, "high": 2}


def classify(*texts: str) -> tuple[str, str]:
    """-> (risk, why) over the question, header and option labels/descriptions."""
    blob = " ".join(t for t in texts if t)
    m = HIGH.search(blob)
    level, why = ("high", f"rule: '{m.group(0)}'") if m else (None, "")
    if not level:
        m = MED.search(blob)
        level, why = ("med", f"rule: '{m.group(0)}'") if m else ("low", "no rule matched")
    for r in RAISE.findall(blob):
        r = "med" if r.lower().startswith("med") else r.lower()
        if ORDER[r] > ORDER[level]:
            level, why = r, f"raised by the chat to {r}"
    return level, why


def of_parts(parts: list[dict]) -> tuple[str, str]:
    """Highest risk over all sub-questions of one AskUserQuestion call."""
    best, why = "low", "no rule matched"
    for p in parts:
        r, w = classify(p.get("question", ""), p.get("header", ""),
                        *[o.get("label", "") + " " + o.get("desc", "") for o in p.get("options", [])])
        if ORDER[r] > ORDER[best]:
            best, why = r, w
    return best, why
