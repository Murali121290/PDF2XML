from pdf2xml.model import Document, Heading, ListBlock, ListItem, Meta, Paragraph, Run, iter_blocks
from pdf2xml.xml.serialize import to_xml, validate


def _doc() -> Document:
    return Document(
        meta=Meta(source="x.pdf", sha256="0" * 64, pages=1),
        body=[
            Heading(id="b1", page=1, bbox=(1.004, 2, 3, 4), level=1, runs=[Run(text="Hi")]),
            Paragraph(id="b2", page=1, bbox=(0, 0, 1, 1),
                      runs=[Run(text="a "), Run(text="b", bold=True, color="#ff0000")]),
            ListBlock(id="b3", page=1, bbox=(0, 0, 1, 1), items=[
                ListItem(id="b4", page=1, bbox=(0, 0, 1, 1), marker="•", runs=[Run(text="x")],
                         children=[ListBlock(id="b5", page=1, bbox=(0, 0, 1, 1), items=[
                             ListItem(id="b6", page=1, bbox=(0, 0, 1, 1), runs=[Run(text="y")])
                         ])]),
            ]),
        ],
    )


def test_json_round_trip_is_identical() -> None:
    doc = _doc()
    again = Document.model_validate_json(doc.model_dump_json())
    assert again == doc


def test_bbox_rounded_to_two_decimals() -> None:
    assert _doc().body[0].bbox == (1.0, 2, 3, 4)


def test_iter_blocks_is_depth_first() -> None:
    assert [b.id for b in iter_blocks(_doc().body)] == ["b1", "b2", "b3", "b4", "b5", "b6"]


def test_xml_is_schema_valid_with_nested_lists() -> None:
    root = to_xml(_doc())
    assert validate(root) == []
    r = root.find("body/p/r")
    assert r is not None and r.get("b") == "true" and r.get("color") == "#ff0000"
