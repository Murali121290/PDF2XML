from pdf2xml.merge.text import build_runs, fix_ligatures, group_lines, runs_text
from tests.conftest import line_of, make_word


def test_lines_are_ordered_top_to_bottom_left_to_right() -> None:
    words = line_of(["second", "line"], 50, 30) + line_of(["first", "line"], 50, 10)
    lines = group_lines(words)
    assert [" ".join(w.text for w in ln.words) for ln in lines] == ["first line", "second line"]


def test_space_inserted_only_for_real_gaps() -> None:
    h = make_word("H", 50, 10)
    two = make_word("2", h.bbox[2], 16, size=6)          # touching, below baseline → subscript
    o = make_word("O", two.bbox[2], 10)
    res = build_runs(group_lines([h, two, o]))
    assert runs_text(res.runs) == "H2O"
    assert [r.sub for r in res.runs] == [False, True, False]


def test_superscript_detected() -> None:
    e = line_of(["E", "=", "mc"], 50, 10)
    sq = make_word("2", e[-1].bbox[2], 8, size=6)       # raised
    res = build_runs(group_lines([*e, sq]))
    assert runs_text(res.runs) == "E = mc2"
    assert res.runs[-1].sup and res.runs[-1].text == "2"


def test_style_changes_split_runs_and_spaces_stay_outside_bold() -> None:
    words = (line_of(["a"], 50, 10) + line_of(["bold"], 60, 10, bold=True)
             + line_of(["word"], 90, 10))
    res = build_runs(group_lines(words))
    assert [(r.text, r.bold) for r in res.runs] == [("a ", False), ("bold ", True), ("word", False)]


def test_dehyphenation_joins_lowercase_continuation() -> None:
    words = line_of(["an", "infor-"], 50, 10) + line_of(["mation", "system"], 50, 24)
    res = build_runs(group_lines(words))
    assert runs_text(res.runs) == "an information system"
    assert res.dehyphenated == 1


def test_hyphen_kept_before_capital() -> None:
    words = line_of(["pre-"], 50, 10) + line_of(["Raphaelite"], 50, 24)
    res = build_runs(group_lines(words))
    assert runs_text(res.runs) == "pre- Raphaelite"
    assert res.dehyphenated == 0


def test_code_keeps_line_breaks() -> None:
    words = line_of(["x", "=", "1"], 50, 10) + line_of(["y", "=", "2"], 50, 24)
    res = build_runs(group_lines(words), preserve_lines=True)
    assert runs_text(res.runs) == "x = 1\ny = 2"


def test_ligatures_expanded() -> None:
    assert fix_ligatures("ﬁle ﬂow") == "file flow"
