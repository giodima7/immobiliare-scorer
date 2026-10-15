"""One-off design-token normaliser for the dashboard (2026-10-15).

  • alias tokens (--surface, --muted, --accent …) → canonical names
  • hard-coded colours within a small distance of a token → var(--token)
  • font sizes snapped to the type scale (no 9 / 9.5 / 11.5 / 15 / 17 / 22 / 26 px)
  • border radii → radius tokens

Run with --dry to see what would change.
"""
import glob
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "dashboard"
DRY = "--dry" in sys.argv

ALIAS = {"bg": "bg-page", "surface": "bg-card", "surf2": "bg-muted", "border": "border-light",
         "accent": "green-deep", "green": "green-mid", "yellow": "amber", "red": "coral",
         "text": "text-primary", "muted": "text-secondary",
         "accent-light": "green-light", "accent-dark": "green-deep"}
TOKENS = {"bg-page": "#F7F5F2", "bg-card": "#FFFFFF", "bg-muted": "#F2EFE9", "bg-pill": "#EDEAE4",
          "green-deep": "#2A7A5A", "green-mid": "#4CAF80", "green-light": "#E6F4ED",
          "amber": "#E8922A", "amber-light": "#FEF3E2", "coral": "#E05C4B", "coral-light": "#FDEEEC",
          "blue-soft": "#4A90D9", "blue-light": "#EBF4FF", "text-primary": "#1A1A1A",
          "text-secondary": "#6B6560", "text-muted": "#A09990", "border-light": "#E8E4DE",
          "border-strong": "#D4CFC8", "green-ink": "#1F6B4C", "coral-ink": "#B42318",
          "amber-ink": "#8A5410", "blue-ink": "#1A5FA8"}
# Tailwind-style shades used for text on tints → the matching ink token.
EXPLICIT = {"#15803d": "green-ink", "#16a34a": "green-ink", "#235f47": "green-ink", "#166534": "green-ink",
            "#047857": "green-ink", "#065f46": "green-ink", "#34d399": "green-mid", "#10b981": "green-mid",
            "#dc2626": "coral-ink", "#c0392b": "coral-ink", "#b91c1c": "coral-ink", "#991b1b": "coral-ink",
            "#ef4444": "coral", "#e74c3c": "coral",
            "#92400e": "amber-ink", "#d97706": "amber-ink", "#b85c00": "amber-ink", "#b45309": "amber-ink",
            "#f59e0b": "amber", "#0e7490": "blue-ink", "#1a5fa8": "blue-ink", "#1d4ed8": "blue-ink",
            "#2563eb": "blue-soft", "#555": "text-secondary", "#555555": "text-secondary", "#666": "text-secondary",
            "#999": "text-muted", "#888": "text-muted", "#333": "text-primary", "#222": "text-primary",
            "#eee": "border-light", "#ddd": "border-strong", "#ccc": "border-strong", "#fff": "bg-card", "#ffffff": "bg-card"}
MAX_DIST = 22          # RGB distance under which a colour is "the same" as a token
FONT_MAP = {"9px": "11px", "9.5px": "11px", "10px": "11px", "10.5px": "11px", "11.5px": "12px",
            "12.5px": "13px", "15px": "14px", "17px": "18px", "22px": "24px", "26px": "28px"}


def rgb(h):
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def nearest(h):
    r = rgb(h)
    best = min(TOKENS.items(), key=lambda kv: sum((a - b) ** 2 for a, b in zip(r, rgb(kv[1]))))
    return best[0], sum((a - b) ** 2 for a, b in zip(r, rgb(best[1]))) ** 0.5


def radius_token(px):
    v = float(px)
    if v <= 5:
        return "var(--radius-xs)"
    if v <= 10:
        return "var(--radius-sm)"
    if v <= 16:
        return "var(--radius-md)"
    if v <= 24:
        return "var(--radius-lg)"
    if v >= 99:
        return "var(--radius-full)"
    return "var(--radius-xl)"


stats = Counter()
kept = Counter()
files = [ROOT / "assets" / "lume.css", ROOT / "index.html"] + [Path(p) for p in sorted(glob.glob(str(ROOT / "assets" / "*.js")))]
for path in files:
    src = path.read_text()
    out = src
    # 1. alias tokens
    out, n = re.subn(r"var\(--(" + "|".join(map(re.escape, ALIAS)) + r")(?=[,)])",
                     lambda m: f"var(--{ALIAS[m.group(1)]}", out)
    stats["alias"] += n
    is_css = path.suffix == ".css"
    # 2. colours (CSS file, outside :root; style="" attributes elsewhere)
    def colour(m):
        h = m.group(0)
        if h.lower() in EXPLICIT:
            stats["colour"] += 1
            return f"var(--{EXPLICIT[h.lower()]})"
        tok, d = nearest(h)
        if d <= MAX_DIST:
            stats["colour"] += 1
            return f"var(--{tok})"
        kept[h.lower()] += 1
        return h
    if is_css:
        root_end = out.index("}", out.index(":root")) + 1
        body = re.sub(r"(?<![\w-])#[0-9a-fA-F]{6}\b|(?<![\w-])#[0-9a-fA-F]{3}\b(?![0-9a-fA-F])",
                      colour, out[root_end:])
        out = out[:root_end] + body
    else:
        out = re.sub(r'style="[^"]*"', lambda m: re.sub(r"#[0-9a-fA-F]{6}\b", colour, m.group(0)), out)
    # 3. font sizes
    out, n = re.subn(r"font-size:\s*(9|9\.5|10|10\.5|11\.5|12\.5|15|17|22|26)px",
                     lambda m: "font-size: " + FONT_MAP[m.group(1) + "px"], out)
    stats["font"] += n
    # 4. radii (CSS only; single px values and px lists)
    if is_css:
        def rad(m):
            vals = m.group(1).split()
            if not all(re.fullmatch(r"\d+(\.\d+)?px", v) for v in vals):
                return m.group(0)
            stats["radius"] += 1
            return "border-radius: " + " ".join(radius_token(v[:-2]) for v in vals) + ";"
        out = re.sub(r"border-radius:\s*([^;}]+);", rad, out)
    if out != src and not DRY:
        path.write_text(out)

print(dict(stats))
print("colours left as is:", sum(kept.values()), "distinct", len(kept), kept.most_common(15))
