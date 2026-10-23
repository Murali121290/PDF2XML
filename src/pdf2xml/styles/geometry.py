"""Paragraph geometry measured from line boxes: alignment, indents, line height."""

from __future__ import annotations

import statistics
from collections.abc import Sequence

from pdf2xml.merge.text import Line
from pdf2xml.model import ParaProps

_TOL = 2.0


def _spread(vals: Sequence[float]) -> float:
    return max(vals) - min(vals) if vals else 0.0


def para_props(lines: Sequence[Line], area: tuple[float, float]) -> ParaProps | None:
    """``area`` = (x0, x1) of the text column/page area the block sits in."""
    lines = [ln for ln in lines if ln.words and ln.words[0].upright]
    if not lines:
        return None
    ax0, ax1 = area
    p = ParaProps()
    if len(lines) == 1:
        ln = lines[0]
        centre = (ln.x0 + ln.x1) / 2
        if abs(centre - (ax0 + ax1) / 2) < 3 and ln.x0 - ax0 > 10:
            p.align = "center"
        elif abs(ln.x1 - ax1) < _TOL and ln.x0 - ax0 > 20:
            p.align = "right"
        else:
            p.align = "left"
        return p

    lefts = [ln.x0 for ln in lines]
    rights = [ln.x1 for ln in lines]
    centres = [(ln.x0 + ln.x1) / 2 for ln in lines]
    left_ok = _spread(lefts[1:]) < _TOL
    right_ok = _spread(rights[:-1]) < _TOL
    if left_ok and right_ok:
        # With two lines, a short last line cannot tell justified from ragged-right text.
        p.align = "justify" if len(lines) >= 3 else "left"
    elif _spread(rights) < _TOL and not left_ok:
        p.align = "right"
    elif _spread(centres) < _TOL and not left_ok:
        p.align = "center"
    else:
        p.align = "left"

    if p.align in ("left", "justify"):
        first = lefts[0] - statistics.median(lefts[1:])
        if abs(first) > 1.0:
            p.indent_first = round(first, 1)

    tops = [ln.top for ln in lines]
    steps = [b - a for a, b in zip(tops, tops[1:], strict=False) if b > a]
    size = statistics.median(ln.size for ln in lines)
    if steps and size > 0:
        p.line_height = round(statistics.median(steps) / size, 2)
    return p
