"""Generate small, redistributable fixture PDFs with reportlab.

    uv run python tests/fixtures/make_fixtures.py

The PDFs are committed; re-run only when a fixture needs to change, then review golden diffs.
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    ListFlowable,
    ListItem,
    PageTemplate,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.platypus import (
    Image as RLImage,
)

OUT = Path(__file__).parent / "pdfs"

TITLE = ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=20, leading=24,
                       spaceAfter=10, textColor=colors.HexColor("#1a3c6e"))
H2 = ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=14, leading=17, spaceBefore=10,
                    spaceAfter=6, textColor=colors.HexColor("#1a3c6e"))
H3 = ParagraphStyle("h3", fontName="Helvetica-BoldOblique", fontSize=11.5, leading=14,
                    spaceBefore=8, spaceAfter=4)
BODY = ParagraphStyle("body", fontName="Times-Roman", fontSize=10.5, leading=13.5,
                      alignment=TA_JUSTIFY, spaceAfter=6)
CAPTION = ParagraphStyle("caption", fontName="Times-Italic", fontSize=9, leading=11,
                         alignment=TA_CENTER, spaceAfter=8)

LOREM = (
    "Born-digital documents keep their text as real characters, so a converter can read the "
    "exact font, size and colour of every word. The hard part is recovering structure: which "
    "lines form a paragraph, where a heading starts, and in which order the columns are read. "
)


def _figure_png() -> io.BytesIO:
    img = Image.new("RGB", (400, 200), (235, 242, 250))
    d = ImageDraw.Draw(img)
    for i, h in enumerate((60, 120, 90, 160, 110)):
        d.rectangle([40 + i * 70, 190 - h, 90 + i * 70, 190], fill=(26, 60, 110))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


def _header_footer(canvas, doc) -> None:  # type: ignore[no-untyped-def]
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.HexColor("#666666"))
    canvas.drawString(20 * mm, A4[1] - 12 * mm, "Sample Report - Converter Fixtures")
    canvas.drawCentredString(A4[0] / 2, 10 * mm, str(doc.page))
    canvas.restoreState()


def simple() -> Path:
    path = OUT / "simple" / "simple.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(str(path), pagesize=A4, leftMargin=25 * mm, rightMargin=25 * mm,
                            topMargin=22 * mm, bottomMargin=20 * mm, title="Simple Fixture")
    story = [
        Paragraph("Converting PDF to XML", TITLE),
        Paragraph("1. Introduction", H2),
        Paragraph(
            LOREM + "This sentence has <b>bold words</b>, <i>italic words</i> and a "
            '<font color="#c00000">red warning</font>. Water is H<sub>2</sub>O and '
            "energy is E = mc<super>2</super>.", BODY),
        Paragraph(LOREM * 2, BODY),
        Paragraph("1.1 Goals", H3),
        ListFlowable(
            [ListItem(Paragraph(t, BODY), leftIndent=12) for t in (
                "Keep every character of the text layer.",
                "Keep styles: font, size, colour, bold and italic.",
                "Produce valid XML, EPUB and DOCX.")],
            bulletType="bullet", start="•", leftIndent=14,
        ),
        Paragraph("2. Method", H2),
        Paragraph(LOREM * 3, BODY),
        ListFlowable(
            [ListItem(Paragraph(t, BODY)) for t in (
                "Extract words with their styles.",
                "Detect blocks and reading order.",
                "Merge both layers by position.")],
            bulletType="1", bulletFormat="%s.", leftIndent=16,
        ),
        RLImage(_figure_png(), width=120 * mm, height=60 * mm),
        Paragraph("Figure 1. Characters kept per stage.", CAPTION),
        Paragraph(LOREM * 4, BODY),
        Paragraph("3. Results", H2),
        Paragraph(LOREM * 5, BODY),
        Paragraph(LOREM * 5, BODY),
    ]
    doc.build(story, onFirstPage=_header_footer, onLaterPages=_header_footer)
    return path


def two_column() -> Path:
    path = OUT / "paper" / "two_column.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = BaseDocTemplate(str(path), pagesize=A4, title="Two Column Fixture")
    lm, rm, tm, bm, gutter = 18 * mm, 18 * mm, 20 * mm, 20 * mm, 8 * mm
    w, h = A4
    col_w = (w - lm - rm - gutter) / 2
    title_h = 30 * mm
    first = [
        Frame(lm, h - tm - title_h, w - lm - rm, title_h, id="title"),
        Frame(lm, bm, col_w, h - tm - bm - title_h, id="c1"),
        Frame(lm + col_w + gutter, bm, col_w, h - tm - bm - title_h, id="c2"),
    ]
    later = [
        Frame(lm, bm, col_w, h - tm - bm, id="c1b"),
        Frame(lm + col_w + gutter, bm, col_w, h - tm - bm, id="c2b"),
    ]
    doc.addPageTemplates([
        PageTemplate("first", first, onPage=_header_footer, autoNextPageTemplate="later"),
        PageTemplate("later", later, onPage=_header_footer),
    ])
    body = ParagraphStyle("b2", parent=BODY, fontSize=9.5, leading=12)
    story = [
        Paragraph("A Two-Column Study of Reading Order", ParagraphStyle(
            "t2", parent=TITLE, alignment=TA_CENTER)),
        Paragraph("<i>Abstract</i> - " + LOREM, ParagraphStyle("abs", parent=body,
                                                               alignment=TA_CENTER)),
    ]
    for n in range(1, 5):
        story.append(Paragraph(f"{n}. Section {n}", H2))
        story.extend(Paragraph(f"[{n}.{k}] " + LOREM * 2, body) for k in range(1, 4))
    doc.build(story)
    return path


def table() -> Path:
    path = OUT / "simple" / "table.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(str(path), pagesize=A4, title="Table Fixture")
    data = [
        ["Tool", "Layer", "Licence"],
        ["Docling", "Structure", "MIT"],
        ["pdfplumber", "Style", "MIT"],
        ["GROBID", "Papers", "Apache-2.0"],
    ]
    t = Table(data, colWidths=[45 * mm, 45 * mm, 45 * mm])
    t.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 10),
        ("FONT", (0, 1), (-1, -1), "Helvetica", 10),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dde6f0")),
    ]))
    story = [
        Paragraph("Tool Comparison", TITLE),
        Paragraph("The table below lists the tools used by each layer.", BODY),
        Spacer(1, 6),
        Paragraph("Table 1. Tools and licences.", CAPTION),
        t,
        Spacer(1, 10),
        Paragraph("All three tools are open source.", BODY),
    ]
    doc.build(story)
    return path


if __name__ == "__main__":
    for fn in (simple, two_column, table):
        print("wrote", fn())
