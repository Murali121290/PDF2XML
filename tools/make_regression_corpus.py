"""Generate deterministic born-digital PDFs and machine-readable acceptance references."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pikepdf


def make_corpus(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    cases = []
    samples = {
        "simple": [(72, 720, 20, "Regression document"),
                   (72, 680, 12, "The first paragraph preserves every source word."),
                   (72, 650, 12, "The second paragraph follows the first paragraph.")],
        "columns": [(72, 740, 20, "Two column reference"),
                    (72, 680, 12, "Left column begins here."), (72, 655, 12, "Left column ends here."),
                    (330, 680, 12, "Right column begins here."), (330, 655, 12, "Right column ends here.")],
        "table": [(72, 740, 20, "Table reference"),
                  (85, 685, 12, "Product"), (285, 685, 12, "Quantity"),
                  (85, 645, 12, "Apples"), (285, 645, 12, "12"),
                  (85, 605, 12, "Pears"), (285, 605, 12, "24")],
    }
    for name, rows in samples.items():
        with pikepdf.Pdf.new() as pdf:
            page = pdf.add_blank_page(page_size=(612, 792))
            font = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Font,
                Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Helvetica))
            page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
            commands = [f"BT /F1 {size} Tf {x} {y} Td ({text}) Tj ET" for x, y, size, text in rows]
            if name == "table":
                commands += [f"72 {y} m 472 {y} l S" for y in (710, 670, 630, 590)]
                commands += [f"{x} 590 m {x} 710 l S" for x in (72, 272, 472)]
            page.Contents = pdf.make_stream("\n".join(commands).encode("ascii"))
            pdf.save(folder / f"{name}.pdf")
        cases.append({"pdf": f"{name}.pdf", "expected_text": [r[3] for r in rows],
                      "expected_tables": 1 if name == "table" else 0,
                      "table_cells": ["Product", "Quantity", "Apples", "12", "Pears", "24"] if name == "table" else []})
    manifest = folder / "corpus.json"
    manifest.write_text(json.dumps({"synthetic": True, "cases": cases}, indent=2), encoding="utf-8")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    print(make_corpus(parser.parse_args().folder))
