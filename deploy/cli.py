"""pdf2xml (deploy edition): PDF → Docling → canonical JSON → canonical XML → BITS → JATS.

The deploy package holds only this conversion. It runs offline: the Docling models ship in
the package's ``models`` folder, and nothing is downloaded.

    python -m pdf2xml book.pdf chapter.pdf -o output
    python -m pdf2xml C:\\pdfs --pages "1-20" --profile house.yaml --meta book.yaml
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

from pdf2xml import __version__

TARGETS = ("json", "xml", "bits", "jats")
APP_HOME = Path(os.environ.get("PDF2XML_HOME") or Path(__file__).resolve().parents[4])


def _inputs(paths: list[str]) -> list[Path]:
    """PDF files, and the PDFs inside folders, in name order."""
    out: list[Path] = []
    for p in map(Path, paths):
        if p.is_dir():
            out.extend(sorted(x for x in p.iterdir() if x.suffix.lower() == ".pdf"))
        elif p.is_file() and p.suffix.lower() == ".pdf":
            out.append(p)
        else:
            raise SystemExit(f"error: not a PDF file or folder: {p}")
    if not out:
        raise SystemExit("error: no PDF files found")
    return out


def _models() -> Path:
    from pdf2xml.extract.structure_docling import resolve_models_dir

    folder = Path(os.environ.get("PDF2XML_MODELS_DIR") or APP_HOME / "models")
    try:
        found = resolve_models_dir(folder)
    except ValueError as e:
        raise SystemExit(f"error: {e}") from None
    assert found is not None
    return found


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="pdf2xml", description="Convert born-digital PDFs to canonical JSON/XML, BITS "
        "2.2 and JATS 1.4 with Docling (offline).")
    ap.add_argument("inputs", nargs="+", help="PDF file(s) or folder(s) of PDFs")
    ap.add_argument("-o", "--out", type=Path,
                    default=Path(os.environ.get("PDF2XML_OUT") or APP_HOME / "output"),
                    help="output folder; each PDF gets <out>/<pdf name>/ (default: %(default)s)")
    ap.add_argument("--to", default=",".join(TARGETS),
                    help="comma list of json,xml,bits,jats (default: all four)")
    ap.add_argument("--pages", help='page selection, e.g. "1-3,7" (default: all pages)')
    ap.add_argument("--profile", type=Path, help="BITS/JATS export profile (YAML or JSON)")
    ap.add_argument("--meta", type=Path, help="book/journal/chapter metadata (YAML or JSON)")
    ap.add_argument("--no-cache", action="store_true",
                    help="run Docling again instead of reusing 11_structure_raw.json")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    ap.add_argument("--version", action="version", version=f"pdf2xml {__version__} (deploy)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    targets = [t.strip() for t in args.to.split(",") if t.strip()]
    bad = [t for t in targets if t not in TARGETS]
    if bad or not targets:
        ap.error(f"--to takes {','.join(TARGETS)}; got {args.to!r}")
    for opt in (args.profile, args.meta):
        if opt is not None and not opt.is_file():
            ap.error(f"file not found: {opt}")

    from pdf2xml.config import Config, parse_pages
    from pdf2xml.pipeline import ConversionError, convert

    pdfs = _inputs(args.inputs)
    cfg = Config(engine="docling", targets=targets,  # type: ignore[arg-type]
                 pages=parse_pages(args.pages), profile=args.profile, metadata=args.meta,
                 overlays=False, completeness=False, reuse_cache=not args.no_cache,
                 models_dir=_models(), grobid_url=None)
    args.out.mkdir(parents=True, exist_ok=True)
    print(f"pdf2xml {__version__} (deploy): {len(pdfs)} PDF(s) → {args.out.resolve()}")
    failed = 0
    for pdf in pdfs:
        t = time.perf_counter()
        try:
            res = convert(pdf, args.out, cfg)
        except ConversionError as e:
            print(f"FAILED {pdf.name}: {e}", file=sys.stderr)
            failed += 1
            continue
        except Exception as e:                          # keep going with the next PDF
            logging.exception("%s", pdf.name)
            print(f"FAILED {pdf.name}: {type(e).__name__}: {e}", file=sys.stderr)
            failed += 1
            continue
        cov = res.coverage
        parts = [f"coverage={cov.ratio:.4f}", f"xml_valid={not res.xml_errors}"]
        parts += [f"{k}_valid={r.valid}" for k, r in res.exports.items()]
        ok = cov.ok and not res.xml_errors and all(r.valid for r in res.exports.values())
        print(f"{'OK    ' if ok else 'CHECK '} {pdf.name} → {res.out_dir}  "
              f"({' '.join(parts)}, {time.perf_counter() - t:.0f} s)")
        if not ok:
            print("        see report.json in that folder")
    if failed:
        print(f"{failed} of {len(pdfs)} PDF(s) failed", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
