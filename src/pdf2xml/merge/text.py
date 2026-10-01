"""Turning positioned words into lines and styled runs."""

from __future__ import annotations

import re
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import lru_cache

from pdf2xml.extract.types import Word
from pdf2xml.styles.fonts import parse_font

_LIGATURES = {
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl",
    "ﬅ": "st", "ﬆ": "st",
}
_LIG_RE = re.compile("[" + "".join(_LIGATURES) + "]")
SOFT_HYPHEN = "­"

# A list marker as a whole token: bullets, dashes, 1. 1) (1) a. a) i. ii), or a single glyph
# without a Unicode mapping ("(cid:127)"), which in practice is almost always a bullet.
MARKER_RE = re.compile(
    r"^(?:[•‣⁃∙▪▫●○◦■□➢✓✔"
    r"–—*\-]|\(?\d{1,3}[.)]|\(?[a-zA-Z][.)]|\(?[ivxlcdmIVXLCDM]{1,5}[.)]|\(cid:\d+\))$"
)
CID_RE = re.compile(r"\(cid:\d+\)")
_ORDERED_RE = re.compile(r"^\(?(\d{1,3}|[a-zA-Z]|[ivxlcdmIVXLCDM]{1,5})[.)]$")


def is_marker(token: str) -> bool:
    return bool(MARKER_RE.match(token))


def is_ordered_marker(token: str) -> bool:
    return bool(_ORDERED_RE.match(token))


def fix_ligatures(text: str) -> str:
    return _LIG_RE.sub(lambda m: _LIGATURES[m.group(0)], text)


# ---------------------------------------------------------------- runs (absolute formatting)

@dataclass
class ARun:
    """A run with *absolute* formatting (the catalogue step later reduces it to differences)."""

    text: str
    family: str
    size: float
    bold: bool
    italic: bool
    color: str
    mono: bool
    generic: str = "serif"
    sup: bool = False
    sub: bool = False

    def sig(self) -> tuple[str, float, bool, bool, str]:
        return (self.family, round(self.size * 2) / 2, self.bold, self.italic, self.color)

    def fmt_key(self) -> tuple[object, ...]:
        return (*self.sig(), self.mono, self.sup, self.sub)


@dataclass
class Line:
    words: list[Word] = field(default_factory=list)

    @property
    def x0(self) -> float:
        return min(w.x0 for w in self.words)

    @property
    def x1(self) -> float:
        return max(w.x1 for w in self.words)

    @property
    def top(self) -> float:
        return min(w.top for w in self._base())

    @property
    def bottom(self) -> float:
        return max(w.bottom for w in self._base())

    @property
    def size(self) -> float:
        return max(w.size for w in self.words)

    def _base(self) -> list[Word]:
        m = self.size
        return [w for w in self.words if w.size >= 0.9 * m] or self.words


# ---------------------------------------------------------------- lines

def group_lines(words: Sequence[Word], split_gap: float | None = None) -> list[Line]:
    """Cluster words into visual lines (top-to-bottom, words left-to-right).

    Small raised/lowered words (superscripts, subscripts) are folded into the line they touch.
    With ``split_gap`` (a multiple of font size), a line is cut wherever the horizontal gap
    between words exceeds it, e.g. at a column gutter when grouping a whole page.
    """
    upright = sorted((w for w in words if w.upright), key=lambda w: ((w.top + w.bottom) / 2, w.x0))
    lines: list[Line] = []
    for w in upright:
        cy = (w.top + w.bottom) / 2
        target = None
        for ln in reversed(lines[-4:]):
            h = ln.bottom - ln.top
            if ln.top - 0.1 * h <= cy <= ln.bottom + 0.1 * h and _no_x_overlap(ln, w):
                target = ln
                break
        if target is None:
            lines.append(Line([w]))
        else:
            target.words.append(w)

    # Fold small-print lines (sup/sub) into an overlapping larger line.
    lines.sort(key=lambda ln: ln.size, reverse=True)
    kept: list[Line] = []
    for ln in lines:
        host = next(
            (k for k in kept
             if ln.size < 0.85 * k.size and ln.bottom > k.top and ln.top < k.bottom
             and all(_no_x_overlap(k, w) for w in ln.words)),
            None,
        )
        if host is not None:
            host.words.extend(ln.words)
        else:
            kept.append(ln)
    for ln in kept:
        ln.words.sort(key=lambda w: w.x0)
    if split_gap is not None:
        split: list[Line] = []
        for ln in kept:
            cur = Line([ln.words[0]])
            for w in ln.words[1:]:
                if w.x0 - cur.words[-1].x1 > split_gap * max(w.size, cur.words[-1].size):
                    split.append(cur)
                    cur = Line([w])
                else:
                    cur.words.append(w)
            split.append(cur)
        kept = split
    kept.sort(key=lambda ln: (round(ln.top, 1), ln.x0))

    # Rotated text: one line per word, in content order.
    kept.extend(Line([w]) for w in sorted((w for w in words if not w.upright), key=lambda w: w.seq))
    return kept


def _no_x_overlap(line: Line, w: Word) -> bool:
    return all(w.x0 >= o.x1 - 0.5 or w.x1 <= o.x0 + 0.5 for o in line.words)


# ---------------------------------------------------------------- runs

def _script(w: Word, line: Line) -> tuple[bool, bool]:
    base = line.size
    if w.size >= 0.85 * base:
        return False, False
    base_words = line._base()
    base_bottom = statistics.median(b.bottom for b in base_words)
    if base_bottom - w.bottom > 0.2 * base:
        return True, False
    if w.bottom - base_bottom > 0.1 * base:
        return False, True
    return False, False


@lru_cache(maxsize=512)
def _generic(font: str) -> str:
    return parse_font(font).generic


def _arun(w: Word, text: str, generic: str, sup: bool, sub: bool) -> ARun:
    return ARun(text=text, family=w.family, size=w.size, bold=w.bold, italic=w.italic,
                color=w.color, mono=w.mono, generic=generic, sup=sup, sub=sub)


@dataclass
class RunResult:
    runs: list[ARun]
    dehyphenated: int = 0


def build_runs(
    lines: Sequence[Line],
    *,
    dehyphenate: bool = True,
    preserve_lines: bool = False,
) -> RunResult:
    """Join lines into runs. Adjacent words with identical formatting merge into one run.

    ``dehyphenated`` counts removed end-of-line hyphens (the coverage check allows for them).
    """
    pieces: list[ARun] = []
    joins = 0

    for li, line in enumerate(lines):
        prev: Word | None = None
        for w in line.words:
            sup, sub = _script(w, line)
            text = fix_ligatures(w.text)
            sep = ""
            if prev is not None:
                gap = w.x0 - prev.x1
                if gap > 0.12 * max(prev.size, w.size):
                    sep = " "
            if sep and pieces and (pieces[-1].sup or pieces[-1].sub) and not (sup or sub):
                text = sep + text          # keep the space outside the raised/lowered run
            elif sep and pieces:
                pieces[-1].text += sep
            pieces.append(_arun(w, text, _generic(w.font), sup, sub))
            prev = w

        if li == len(lines) - 1 or not pieces:
            continue
        # Line break → separator between this line and the next.
        nxt = lines[li + 1].words[0].text if lines[li + 1].words else ""
        last = pieces[-1]
        if preserve_lines:
            last.text += "\n"
        elif last.text.endswith(SOFT_HYPHEN):
            last.text = last.text[:-1]
        elif (
            dehyphenate
            and len(last.text) > 2
            and last.text.endswith("-")
            and last.text[-2].isalpha()
            and nxt[:1].islower()
        ):
            last.text = last.text[:-1]
            joins += 1
        else:
            last.text += " "

    runs: list[ARun] = []
    for p in pieces:
        p.text = p.text.replace(SOFT_HYPHEN, "")
        if runs and runs[-1].fmt_key() == p.fmt_key():
            runs[-1].text += p.text
        elif p.text:
            runs.append(p)
    if runs:
        runs[-1].text = runs[-1].text.rstrip(" ")
    return RunResult(runs=runs, dehyphenated=joins)


def runs_text(runs: Sequence[ARun]) -> str:
    return "".join(r.text for r in runs)
