import pytest

from pdf2xml.styles.fonts import family_of, parse_font, to_hex


@pytest.mark.parametrize(
    ("name", "family", "bold", "italic", "generic"),
    [
        ("ABCDEF+TimesNewRomanPS-BoldItalicMT", "Times New Roman", True, True, "serif"),
        ("Times-Roman", "Times New Roman", False, False, "serif"),
        ("Helvetica-Oblique", "Arial", False, True, "sans-serif"),
        ("Helvetica-BoldOblique", "Arial", True, True, "sans-serif"),
        ("QWERTY+MinionPro-It", "Minion Pro", False, True, "serif"),
        ("ArialMT", "Arial", False, False, "sans-serif"),
        ("Courier-Bold", "Courier New", True, False, "monospace"),
        ("XYZABC+SourceSansPro-Semibold", "Source Sans Pro", True, False, "sans-serif"),
    ],
)
def test_parse_font(name: str, family: str, bold: bool, italic: bool, generic: str) -> None:
    info = parse_font(name)
    assert (info.family, info.bold, info.italic, info.generic) == (family, bold, italic, generic)


def test_family_strips_subset_prefix() -> None:
    assert family_of("ABCDEF+Garamond") == "Garamond"


@pytest.mark.parametrize(
    ("color", "hex_"),
    [
        (None, "#000000"),
        ((0,), "#000000"),
        ((1,), "#ffffff"),
        ((1, 0, 0), "#ff0000"),
        ((0, 0, 0, 1), "#000000"),
        ((0, 1, 1, 0), "#ff0000"),
        ("pattern", "#000000"),
        (0.5, "#808080"),
    ],
)
def test_to_hex(color: object, hex_: str) -> None:
    assert to_hex(color) == hex_
