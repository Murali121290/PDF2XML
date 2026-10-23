"""Nest flat lists by marker kind and indent.

Structure engines often report a list with sub-items as one flat run of items ("1.", "2.", "•",
"•", "3."). The printed page shows the nesting in two ways: the sub-items use a different kind of
marker, and they are indented. ``nest_list`` rebuilds the tree from both signals; lists the engine
already nested are left alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pdf2xml.model import BBox, ListBlock, ListItem

_ROMAN = re.compile(r"[ivxlc]+|[IVXLC]+")
_INDENT = 3.0          # pt: smaller x offsets are alignment noise
_SAME_GLYPH_INDENT = 12.0
_COLUMN = 100.0        # pt: a larger x jump is the next column, not an indent


def marker_kind(marker: str | None) -> str:
    """"1." → "num.", "(a)" → "(alpha)", "ii)" → "roman)", "•" → "•"; "" when there is none."""
    if not marker:
        return ""
    t = marker.strip()
    core = t.strip("().[] ")
    wrap = ("(" if t.startswith("(") else "") + (t[-1] if t[-1] in ".)]" else "")
    if core.isdigit():
        return "num" + wrap
    if _ROMAN.fullmatch(core) and (len(core) > 1 or core in "iI"):
        return ("roman" if core.islower() else "ROMAN") + wrap
    if len(core) == 1 and core.isalpha():
        return ("alpha" if core.islower() else "ALPHA") + wrap
    if t in ("-", "–", "—"):
        return "dash"
    return t


def _value(marker: str) -> int | None:
    core = marker.strip().strip("().[] ")
    if core.isdigit():
        return int(core)
    if _ROMAN.fullmatch(core):
        vals = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100}
        n = 0
        low = core.lower()
        for i, ch in enumerate(low):
            v = vals[ch]
            n += -v if i + 1 < len(low) and vals[low[i + 1]] > v else v
        return n
    if len(core) == 1 and core.isalpha():
        return ord(core.lower()) - ord("a") + 1
    return None


def follows(prev: str | None, marker: str | None) -> bool:
    """``marker`` is the next number/letter after ``prev`` ("2." after "1.", "c)" after "b)")."""
    if not prev or not marker:
        return False
    a, b = _value(prev), _value(marker)
    if a is None or b is None:
        return False
    if b == a + 1:
        return True
    # "i" after "h" is a letter, not a Roman numeral
    return prev.strip().strip("().[] ").lower() == "h" and marker.strip().strip("().[] ") in "iI"


def ordered_kind(kind: str) -> bool:
    return kind.lower().startswith(("num", "alpha", "roman", "(num", "(alpha", "(roman"))


@dataclass
class _Level:
    kind: str
    x0: float
    block: ListBlock
    last: str | None = None
    items: list[ListItem] = field(default_factory=list)


def _union(a: BBox, b: BBox) -> BBox:
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def nest_list(lb: ListBlock) -> ListBlock:
    """Return ``lb`` with sub-items moved under the item they belong to (``lb`` is unchanged)."""
    items = lb.items
    kinds = [marker_kind(it.marker) for it in items]
    if (len(items) < 2 or any(it.children for it in items)
            or len({k for k in kinds if k}) < 2 and not _indented_same_glyph(items, kinds)):
        return lb
    root = lb.model_copy(update={"items": []})
    stack = [_Level(kinds[0], items[0].bbox[0], root)]
    for it, kind in zip(items, kinds, strict=True):
        it = it.model_copy(update={"children": []})
        x = it.bbox[0]
        top = stack[-1]
        target = None
        if kind:
            matches = [i for i, lv in enumerate(stack) if lv.kind == kind and (
                not ordered_kind(kind) or lv.last is None or follows(lv.last, it.marker))]
            if matches:
                # the same glyph can mark two levels: take the one indented most like this item
                target = min(reversed(matches), key=lambda i: abs(x - stack[i].x0)
                             if abs(x - stack[i].x0) < _COLUMN else _COLUMN)
        same_column = 0 <= x - top.x0 < _COLUMN
        if (target == len(stack) - 1 and not ordered_kind(kind) and top.items
                and same_column and x - top.x0 >= _SAME_GLYPH_INDENT):
            target = None                                    # same bullet, clearly indented
        if target is not None:
            del stack[target + 1:]
        elif kind and top.items and same_column and x - top.x0 > _INDENT:
            sub = ListBlock(page=it.page, bbox=it.bbox, ordered=ordered_kind(kind), items=[])
            top.items[-1].children.append(sub)
            stack.append(_Level(kind, x, sub))
        lv = stack[-1]
        if not lv.items:
            lv.x0 = x
        elif not same_column:
            lv.x0 = x                                        # continued in the next column
        lv.items.append(it)
        lv.block.items.append(it)
        lv.block.bbox = _union(lv.block.bbox, it.bbox) if len(lv.block.items) > 1 else it.bbox
        lv.last = it.marker
    root.bbox = lb.bbox
    return root


def _indented_same_glyph(items: list[ListItem], kinds: list[str]) -> bool:
    """One bullet glyph throughout, but some items are clearly indented under others."""
    if not kinds[0] or ordered_kind(kinds[0]):
        return False
    xs = [it.bbox[0] for it in items]
    return any(_SAME_GLYPH_INDENT <= b - a < _COLUMN for a, b in zip(xs, xs[1:], strict=False))
