#!/usr/bin/env python3
"""B11: WCAG contrast of every text/background token pair the UI uses, both themes, computed from app.css.
    python3 contrast.py        exits 1 if any pair is under its threshold (4.5 body text, 3.0 large/UI marks)"""
import math
import os
import re
import sys

CSS = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "dhi_orbit", "ui", "css", "app.css")).read()


def block(sel_regex):
    m = re.search(sel_regex + r"\s*\{(.*?)\n\s*\}", CSS, re.S | re.M)
    return dict(re.findall(r"--([\w-]+):\s*(oklch\([^)]*\))", m.group(1))) if m else {}


light = block(r"^:root")
dark = block(r":root\[data-theme=\"dark\"\][^{]*")


def oklch_to_srgb(s):
    L, C, H = [float(x.strip("%")) for x in re.findall(r"[\d.]+%?", s)[:3]]
    L = L / 100 if L > 1 else L
    a, b = C * math.cos(math.radians(H)), C * math.sin(math.radians(H))
    l_ = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m_ = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s_ = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3
    r = 4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_
    g = -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_
    bb = -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_
    return [min(1, max(0, v)) for v in (r, g, bb)]


def lum(rgb):
    """oklch_to_srgb returns LINEAR sRGB (the OKLab matrices end in linear light), so relative luminance is
    the plain weighted sum. (First version decoded gamma a second time: dark colours came out far too dark.)"""
    return sum(w * c for w, c in zip((0.2126, 0.7152, 0.0722), rgb))


def ratio(fg, bg):
    a, b = lum(oklch_to_srgb(fg)), lum(oklch_to_srgb(bg))
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


PAIRS = [  # (fg, bg, threshold, where)
    ("ink", "bg", 4.5, "body text"), ("ink", "panel", 4.5, "strip text"), ("ink-2", "panel", 4.5, "secondary text"),
    ("ink-2", "panel-2", 4.5, "secondary on wait column"), ("ink-3", "panel", 4.5, "hints on panels"), ("ink-3", "bg", 4.5, "hints on page"),
    ("ink-3", "panel-2", 4.5, "hints on panel-2"), ("action", "panel", 4.5, "links, recommended label"), ("action-ink", "action", 4.5, "primary button"),
    ("ok", "ok-bg", 4.5, "OK pill"), ("warn", "warn-bg", 4.5, "UNKNOWN pill"), ("bad", "panel", 4.5, "high-risk chip"),
    ("ok", "panel", 4.5, "verified chip"), ("warn", "panel", 4.5, "no-receipt text"), ("bad", "bad-bg", 4.5, "blocked chip on tint"),
    ("field", "panel", 3.0, "text-field borders (WCAG 1.4.11)"), ("field", "bg", 3.0, "text-field borders on page"),
    ("focus", "panel", 3.0, "focus ring"), ("idle", "panel", 3.0, "idle dot"), ("bad", "panel", 3.0, "needs-you ring"),
    # Not checked on purpose: --line-strong button/strip borders. Every button carries a text label, so its border is
    # decorative, not what identifies the control (WCAG 1.4.11 exempts it); text fields use --field instead.
]
fail = 0
for name, t in (("light", light), ("dark", dark)):
    for fg, bg, th, where in PAIRS:
        r = ratio(t[fg], t[bg])
        ok = r >= th
        fail += not ok
        print(f"{name:5s} {'ok  ' if ok else 'FAIL'} {r:5.2f} (need {th}) --{fg} on --{bg}: {where}")

# ---------------------------------------------------------- nebula look (stage 1): glass over a live field
# Every glass, panel, chip and input token is alpha-composited (in gamma sRGB, as browsers blend) over the field
# colours it can sit on: the void (darkest) and gradient centre for windows, and every particle hue as well (the
# brightest areas) for what can pass over the band. The minimum over those colours is what is reported.
NB = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "dhi_orbit", "ui", "css", "nebula.css")).read()


def nb_block(sel_regex):
    m = re.search(sel_regex + r"\s*\{(.*?)\n\s*\}", NB, re.S | re.M)
    return dict(re.findall(r"--([\w-]+):\s*(oklch\([^)]*\))", m.group(1))) if m else {}


def alpha(s):
    m = re.search(r"/\s*([\d.]+)\s*\)", s)
    return float(m.group(1)) if m else 1.0


def enc(c):
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def dec(c):
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def over(fg, bg_lin):
    """fg: oklch string, maybe with alpha; bg_lin: linear sRGB triple. Returns linear sRGB."""
    a, f = alpha(fg), oklch_to_srgb(fg)
    return [dec(a * enc(x) + (1 - a) * enc(y)) for x, y in zip(f, bg_lin)]


def ratio_lin(fg_lin, bg_lin):
    a, b = lum(fg_lin), lum(bg_lin)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


FIELD = ["nb-void", "nb-void-2", "nb-seat", "nb-out", "nb-unk", "nb-chat", "nb-need", "nb-star"]
NB_PAIRS = [  # (fg token, surface, threshold, where); surfaces are built per field colour below
    ("ink", "G", 4.5, "window text on glass"), ("ink-2", "G", 4.5, "secondary on glass"), ("ink-3", "G", 4.5, "hints on glass"),
    ("ink", "P", 4.5, "strip text"), ("ink-2", "P", 4.5, "secondary on strip"), ("ink-3", "P", 4.5, "hints on strip"),
    ("ink-2", "P2", 4.5, "secondary on wait column"), ("ink-3", "P2", 4.5, "hints on wait column"),
    ("ink", "I", 4.5, "reply field text"), ("ink-3", "I", 4.5, "reply placeholder"),
    ("action", "P", 4.5, "links on strip"), ("action", "G", 4.5, "links on glass"),
    ("ok", "P", 4.5, "verified chip"), ("warn", "P", 4.5, "amber text"), ("bad", "P", 4.5, "high-risk chip"),
    ("field", "P", 3.0, "text-field borders"), ("focus", "P", 3.0, "focus ring"), ("idle", "G", 3.0, "idle dot on glass"),
    ("ink", "R", 4.5, "rail text"), ("ink-2", "R", 4.5, "rail secondary"), ("ink", "O", 4.5, "ornament over a strip"),
    ("ink", "K", 4.5, "capsule current"), ("ink-2", "K", 4.5, "capsule links"), ("ink-2", "KP", 4.5, "capsule over a strip (phone)"),
    ("nb-label", "C", 4.5, "field label"), ("nb-label-2", "C", 4.5, "field label secondary"),
    ("ink", "OPT", 4.5, "option label (interior A)"), ("ink-3", "OPT", 4.5, "option description"), ("ink", "REC", 4.5, "recommended option label"),
    ("action", "REC", 4.5, "RECOMMENDED tag"), ("ink-3", "HEAD", 4.5, "window heading meta"), ("glass-on-ink", "INK", 4.5, "primary button (ink fill)"),
    ("nb-label", "V", 4.5, "seat callout figure (chip-less, on the void)"), ("nb-label-2", "V", 4.5, "seat callout name"),
    ("nb-out-ink", "V", 4.5, "NEARLY OUT callout word"), ("nb-unk-ink", "V", 4.5, "UNKNOWN callout word"),
    ("nb-unk", "F", 3.0, "unknown dashed rim and label outline on the void"), ("nb-out", "F", 3.0, "nearly-out label ring on the void"),
]
for name, t, nb in (("light", light, nb_block(r"^:root")), ("dark", dark, nb_block(r":root\[data-theme=\"dark\"\][^{]*"))):
    T = {**t, **nb}
    for fg, surf, th, where in NB_PAIRS:
        worst = None
        # Windows (G, P, P2, I, O, KP) only ever sit over the void gradient: field.js draws inside the band rows
        # only. The rail, capsule and field labels can pass over the band, so they are checked over every colour.
        for f in (FIELD if surf in ("R", "K", "C") else ["nb-void", "nb-void-2"]):
            F = oklch_to_srgb(T[f])
            G = over(T["glass"], F); P = over(T["glass-panel"], G)
            S = {"F": F, "F0": F, "G": G, "P": P, "P2": over(T["glass-panel-2"], P), "I": over(T["glass-input"], P),
                 "R": over(T["glass-rail"], F), "O": over(T["glass-rail"], P), "K": over(T["glass-capsule"], F),
                 "KP": over(T["glass-capsule"], P), "C": over(T["nb-chip"], F),
                 "OPT": over(T["glass-opt"], P), "REC": over(T["glass-rec"], P), "HEAD": over(T["glass-head"], G),
                 "INK": oklch_to_srgb(T["ink"]), "V": F}
            r = ratio_lin(over(T[fg], S[surf]), S[surf])
            if worst is None or r < worst[0]:
                worst = (r, f)
        ok = worst[0] >= th
        fail += not ok
        print(f"{name:5s} {'ok  ' if ok else 'FAIL'} {worst[0]:5.2f} (need {th}) --{fg} on {surf} over --{worst[1]} (worst field colour): nebula {where}")
print("FAILURES:", fail)
sys.exit(1 if fail else 0)
