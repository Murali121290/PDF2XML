"""Build the style catalogue; reduce runs to differences from their block style (CLAUDE.md §8)."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field

from pdf2xml.merge.merger import MBlock
from pdf2xml.merge.text import ARun, runs_text
from pdf2xml.model import Run, Style

Sig = tuple[str, float, bool, bool, str]

_HEADING_KINDS = {"title", "heading"}
_ROLE_KINDS = ("caption", "code", "footnote", "formula", "reference")
_BODY_KINDS = {"paragraph", "list_item", "other"}


@dataclass
class Catalogue:
    styles: dict[str, Style]
    block_style: dict[int, str] = field(default_factory=dict)   # id(MBlock) → style key
    heading_level: dict[int, int] = field(default_factory=dict)  # id(MBlock) → level
    promoted: int = 0

    def key_of(self, mb: MBlock) -> str | None:
        return self.block_style.get(id(mb))


def _chars(text: str) -> int:
    return sum(1 for c in text if not c.isspace())


def dominant_sig(runs: Sequence[ARun]) -> Sig | None:
    counts: Counter[Sig] = Counter()
    for r in runs:
        if not (r.sup or r.sub):
            counts[r.sig()] += _chars(r.text)
    if not counts:
        return None
    # deterministic tie-break
    return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]


def build_catalogue(blocks: Sequence[MBlock]) -> Catalogue:
    content = [mb for mb in blocks if mb.artifact is None and mb.runs]
    meta: dict[Sig, ARun] = {}
    usage: Counter[Sig] = Counter()
    body_usage: Counter[Sig] = Counter()
    dom: dict[int, Sig] = {}
    for mb in content:
        for r in mb.runs:
            meta.setdefault(r.sig(), r)
            usage[r.sig()] += _chars(r.text)
            if mb.kind in _BODY_KINDS:
                body_usage[r.sig()] += _chars(r.text)
        d = dominant_sig(mb.runs)
        if d is not None:
            dom[id(mb)] = d

    cat = Catalogue(styles={})
    if not usage:
        return cat
    src = body_usage or usage
    body = max(src.items(), key=lambda kv: (kv[1], kv[0]))[0]
    body_size = body[1]

    # Promote short, large-type paragraphs the engine missed to headings.
    for mb in content:
        d = dom.get(id(mb))
        if mb.kind == "paragraph" and d is not None and d[1] >= body_size * 1.3:
            text = runs_text(mb.runs)
            if len(text) < 150 and len(mb.lines) <= 3 and not text.rstrip().endswith("."):
                mb.raw.kind = "heading"
                cat.promoted += 1

    names: dict[Sig, str] = {body: "body"}
    roles: dict[Sig, str] = {body: "body"}

    # Headings: levels from distinct (size, bold) combinations, largest first.
    heading_sigs = {dom[id(mb)] for mb in content if mb.kind in _HEADING_KINDS and id(mb) in dom}
    tiers = sorted({(s[1], s[2]) for s in heading_sigs}, key=lambda t: (-t[0], not t[1]))
    level_of = {t: min(i + 1, 6) for i, t in enumerate(tiers)}
    per_level: dict[int, int] = defaultdict(int)
    for s in sorted(heading_sigs, key=lambda s: (-s[1], not s[2], -usage[s], s)):
        lvl = level_of[(s[1], s[2])]
        per_level[lvl] += 1
        if s not in names:
            names[s] = f"h{lvl}" if per_level[lvl] == 1 else f"h{lvl}-{per_level[lvl]}"
            roles[s] = "heading"

    for kind in _ROLE_KINDS:
        cnt: Counter[Sig] = Counter()
        for mb in content:
            if mb.kind == kind and id(mb) in dom:
                cnt[dom[id(mb)]] += _chars(runs_text(mb.runs))
        if cnt:
            s = max(cnt.items(), key=lambda kv: (kv[1], kv[0]))[0]
            if s not in names:
                names[s] = kind
                roles[s] = kind

    # Remaining block-dominant signatures get neutral names by usage.
    rest = sorted({dom[id(mb)] for mb in content if id(mb) in dom} - names.keys(),
                  key=lambda s: (-usage[s], s))
    for i, s in enumerate(rest, 1):
        names[s] = f"s{i}"

    for s, key in names.items():
        r = meta[s]
        cat.styles[key] = Style(
            font=s[0], size=s[1], bold=s[2], italic=s[3], color=s[4],
            generic=r.generic if r.generic in ("serif", "sans-serif", "monospace")
            else "serif",  # type: ignore[arg-type]
            role=roles.get(s), char_count=usage[s],
        )
        if key.startswith("h") and roles.get(s) == "heading":
            cat.styles[key].level = level_of[(s[1], s[2])]

    for mb in content:
        d = dom.get(id(mb))
        if d is None:
            continue
        cat.block_style[id(mb)] = names[d]
        if mb.kind in _HEADING_KINDS:
            cat.heading_level[id(mb)] = level_of.get((d[1], d[2]), 1)
    return cat


def to_runs(aruns: Sequence[ARun], base: Style | None) -> list[Run]:
    """Absolute runs → canonical runs holding only differences from ``base``."""
    out: list[Run] = []
    for a in aruns:
        r = Run(text=a.text)
        if a.sup:
            r.sup = True
        if a.sub:
            r.sub = True
        if base is None:
            if a.bold:
                r.bold = True
            if a.italic:
                r.italic = True
            if a.color != "#000000":
                r.color = a.color
        else:
            if a.bold != base.bold:
                r.bold = a.bold
            if a.italic != base.italic:
                r.italic = a.italic
            if a.color != base.color:
                r.color = a.color
            if a.family and a.family != base.font:
                r.font = a.family
            if a.size and not (a.sup or a.sub) and abs(a.size - base.size) >= 0.5:
                r.size = round(a.size, 1)
        if out and out[-1].same_format(r):
            out[-1].text += r.text
        else:
            out.append(r)
    return out
