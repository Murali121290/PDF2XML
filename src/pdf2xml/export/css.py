"""CSS generated from the style catalogue (EPUB). Font sizes are em relative to the body style."""

from __future__ import annotations

from pdf2xml.model import Document
from pdf2xml.xml.serialize import num

_BASE = """\
body { margin: 0 5%; }
p, li, h1, h2, h3, h4, h5, h6, pre { margin: 0; }
figure { margin: 1em 0; text-align: center; }
figure img { max-width: 100%; height: auto; }
table { border-collapse: collapse; margin: 1em 0; }
td, th { border: 1px solid #999; padding: 0.2em 0.4em; vertical-align: top; }
pre { white-space: pre-wrap; }
.formula { margin: 0.5em 0; text-align: center; }
section.footnotes { margin-top: 2em; border-top: 1px solid #999; font-size: 0.85em; }
section.references { margin-top: 2em; }
"""


def build_css(doc: Document) -> str:
    body = doc.styles.get("body")
    base = body.size if body else 10.0
    out = [_BASE]
    if body:
        out.append(
            f'body {{ font-family: "{body.font}", {body.generic}; color: {body.color}; }}\n'
        )
    for key in sorted(doc.styles):
        s = doc.styles[key]
        rules = [
            f'font-family: "{s.font}", {s.generic}',
            f"font-size: {num(s.size / base)}em",
            f"font-weight: {'bold' if s.bold else 'normal'}",
            f"font-style: {'italic' if s.italic else 'normal'}",
            f"color: {s.color}",
        ]
        if s.small_caps:
            rules.append("font-variant: small-caps")
        out.append(f".{key} {{ {'; '.join(rules)}; }}\n")
    return "".join(out)
