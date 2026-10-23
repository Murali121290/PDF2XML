"""Font-name parsing, colour conversion and font fallback mapping."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

Generic = Literal["serif", "sans-serif", "monospace"]

_SUBSET = re.compile(r"^[A-Z]{6}\+")
_BOLD = re.compile(r"(bold|black|heavy|semibold|demibold|demi|extrabold|ultrabold|bd\b)", re.I)
_ITALIC = re.compile(r"(italic|oblique|slanted|kursiv|(?<=[a-z-])it\b|(?<=[a-z-])ital)", re.I)
# "Minion-It", "MinionPro-BoldIt", "Garamond,Ital" (case-sensitive: a capital I starts the token)
_IT_SUFFIX = re.compile(r"(?:[-,]|(?<=[a-z]))(?:It|Ital|Ita)(?:MT)?$")
_MONO = re.compile(r"(courier|mono|consol|menlo|inconsolata|typewriter|code|lucidaconsole|cmtt)",
                   re.I)
_SANS = re.compile(r"(arial|helvetica|sans|verdana|tahoma|calibri|segoe|myriad|futura|frutiger|"
                   r"gill|univers|roboto|lato|open ?sans|source ?sans|avenir|franklin|trebuchet|"
                   r"cmss|dejavusans|liberationsans|noto ?sans|inter\b)", re.I)
_STYLE_TOKENS = re.compile(
    r"(bold|black|heavy|semibold|demibold|extrabold|ultrabold|light|thin|medium|regular|roman|"
    r"book|italic|oblique|condensed|narrow|it|bd|bi|mt|ps|psmt|md|lt)$",
    re.I,
)

# Common PDF base names → names installed on most systems / open equivalents.
_KNOWN = {
    "times": "Times New Roman", "timesnewroman": "Times New Roman", "timesroman": "Times New Roman",
    "helvetica": "Arial", "arial": "Arial", "arialmt": "Arial",
    "courier": "Courier New", "couriernew": "Courier New",
    "symbol": "Symbol", "zapfdingbats": "Wingdings",
}


@dataclass(frozen=True)
class FontInfo:
    name: str        # without subset prefix
    family: str      # human family name ("Times New Roman")
    bold: bool
    italic: bool
    mono: bool
    generic: Generic


def strip_subset(name: str) -> str:
    return _SUBSET.sub("", name or "")


def _split_camel(s: str) -> str:
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s)
    s = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def family_of(name: str) -> str:
    """``ABCDEF+TimesNewRomanPS-BoldItalicMT`` → ``Times New Roman``."""
    base = strip_subset(name)
    base = re.split(r"[-,+]", base, maxsplit=1)[0] or base
    # Drop trailing style tokens glued to the family ("ArialBold", "TimesNewRomanPSMT"),
    # stopping as soon as a known family appears ("TimesNewRoman" must keep its "Roman").
    prev = None
    while prev != base:
        key = base.lower().replace(" ", "")
        if key in _KNOWN:
            return _KNOWN[key]
        prev = base
        base = _STYLE_TOKENS.sub("", base) or prev
    return _split_camel(base) or strip_subset(name)


def parse_font(name: str) -> FontInfo:
    clean = strip_subset(name)
    tail = clean.split("-", 1)[1] if "-" in clean else clean.split(",", 1)[-1]
    bold = bool(_BOLD.search(tail)) or bool(_BOLD.search(clean))
    italic = (bool(_ITALIC.search(tail)) or bool(re.search(r"(italic|oblique)", clean, re.I))
              or bool(_IT_SUFFIX.search(clean)))
    mono = bool(_MONO.search(clean))
    generic: Generic = "monospace" if mono else ("sans-serif" if _SANS.search(clean) else "serif")
    return FontInfo(clean, family_of(clean), bold, italic, mono, generic)


def css_font_stack(family: str, generic: str) -> str:
    return f'"{family}", {generic}'


# ---------------------------------------------------------------- colours

def to_hex(color: object) -> str:
    """Convert a pdfplumber/pdfminer colour (gray, RGB or CMYK tuple, 0–1) to ``#rrggbb``.

    Patterns, spot colours and anything unrecognised fall back to black.
    """
    if color is None:
        return "#000000"
    if isinstance(color, int | float):
        vals: Sequence[float] = (float(color),)
    elif isinstance(color, list | tuple) and all(isinstance(c, int | float) for c in color):
        vals = [float(c) for c in color]
    else:
        return "#000000"
    vals = [min(1.0, max(0.0, v)) for v in vals]
    if len(vals) == 1:
        r = g = b = vals[0]
    elif len(vals) == 3:
        r, g, b = vals
    elif len(vals) == 4:
        c, m, y, k = vals
        r, g, b = (1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k)
    else:
        return "#000000"
    return f"#{round(r * 255):02x}{round(g * 255):02x}{round(b * 255):02x}"


def color_distance(a: str, b: str) -> float:
    """Euclidean distance between two ``#rrggbb`` colours (0–441)."""
    ra, ga, ba = (int(a[i:i + 2], 16) for i in (1, 3, 5))
    rb, gb, bb = (int(b[i:i + 2], 16) for i in (1, 3, 5))
    return float(((ra - rb) ** 2 + (ga - gb) ** 2 + (ba - bb) ** 2) ** 0.5)
