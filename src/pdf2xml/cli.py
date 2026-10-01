"""Command line: ``pdf2xml convert``, ``export``, ``validate``, ``inspect``, ``serve``,
``schema``."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from pdf2xml.config import Config, parse_pages

if TYPE_CHECKING:
    from pdf2xml.publishing import ExportResult

app = typer.Typer(add_completion=False,
                  help="Born-digital PDF → canonical JSON/XML → EPUB, DOCX, BITS, JATS")

ProfileOpt = Annotated[Path | None, typer.Option(
    "--profile", exists=True, dir_okay=False, help="BITS/JATS export profile (YAML/JSON)")]
MetaOpt = Annotated[Path | None, typer.Option(
    "--meta", exists=True, dir_okay=False, help="Book/journal/chapter metadata (YAML/JSON)")]


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")


def _targets(to: str) -> list[str]:
    from pdf2xml.config import ALL_TARGETS

    targets = [t.strip() for t in to.split(",") if t.strip()]
    bad = [t for t in targets if t not in ALL_TARGETS]
    if bad:
        raise typer.BadParameter(f"unknown target(s) {bad}; choose from {','.join(ALL_TARGETS)}")
    return targets


def _print_exports(exports: dict[str, ExportResult]) -> None:
    for name, r in exports.items():
        ok = "valid" if r.valid else f"INVALID ({len(r.errors)} errors, see report.json)"
        typer.secho(f"  {name}: {r.path.name} [{r.schema}] {ok}  sections={r.stats['sections']} "
                    f"refs={r.stats['references']} links={r.stats['xrefs'] + r.stats['ext_links']}",
                    fg=None if r.valid else typer.colors.RED)


@app.command()
def convert(
    pdfs: Annotated[list[Path], typer.Argument(exists=True, dir_okay=False, help="PDF file(s)")],
    out: Annotated[Path, typer.Option("--out", "-o", help="Output root directory")] = Path("out"),
    to: Annotated[str, typer.Option(help="Comma list: json,xml,epub,docx,bits,jats")] = (
        "json,xml,epub,docx,bits,jats"),
    profile: ProfileOpt = None,
    meta: MetaOpt = None,
    engine: Annotated[str, typer.Option(help="auto | docling | heuristic")] = "auto",
    style_backend: Annotated[str, typer.Option(help="pdfplumber | pymupdf")] = "pdfplumber",
    pages: Annotated[str | None, typer.Option(help='Page selection, e.g. "1-3,7"')] = None,
    epub_layout: Annotated[str, typer.Option(help="reflow | fixed")] = "reflow",
    overlays: Annotated[bool, typer.Option(help="Write QA overlay PNGs")] = True,
    no_cache: Annotated[bool, typer.Option("--no-cache", help="Re-run Docling")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Convert PDF(s) to canonical JSON/XML, EPUB, DOCX, BITS and JATS."""
    from pdf2xml.pipeline import ConversionError
    from pdf2xml.pipeline import convert as run

    _setup_logging(verbose)
    cfg = Config(
        engine=engine,  # type: ignore[arg-type]
        style_backend=style_backend,  # type: ignore[arg-type]
        targets=_targets(to),  # type: ignore[arg-type]
        profile=profile,
        metadata=meta,
        pages=parse_pages(pages),
        epub_layout=epub_layout,  # type: ignore[arg-type]
        overlays=overlays,
        reuse_cache=not no_cache,
    )
    failed = 0
    for pdf in pdfs:
        try:
            res = run(pdf, out, cfg)
        except ConversionError as e:
            typer.secho(f"✗ {e}", fg=typer.colors.RED, err=True)
            failed += 1
            continue
        cov = res.coverage
        status = "ok" if cov.ok and not res.xml_errors else "check report.json"
        typer.secho(f"✓ {pdf.name} → {res.out_dir}  ({status})",
                    fg=typer.colors.GREEN if status == "ok" else typer.colors.YELLOW)
        typer.echo(f"  blocks={len(res.document.body)} styles={len(res.document.styles)} "
                   f"coverage={cov.ratio:.4f} xml_valid={not res.xml_errors} "
                   f"engine={res.document.meta.engine}")
        _print_exports(res.exports)
        for w in res.document.meta.warnings[:10]:
            typer.echo(f"  ! {w}")
    raise typer.Exit(code=1 if failed else 0)


@app.command()
def export(
    source: Annotated[Path, typer.Argument(
        exists=True, help="A conversion folder (out/<name>) or its 20_canonical.json")],
    to: Annotated[str, typer.Option(help="Comma list: bits,jats")] = "bits,jats",
    profile: ProfileOpt = None,
    meta: MetaOpt = None,
) -> None:
    """Re-export BITS/JATS from an earlier conversion, e.g. with another profile or metadata."""
    import json as _json

    from pdf2xml.model import Document
    from pdf2xml.pipeline import export_summary, publish

    _setup_logging(False)
    path = source / "20_canonical.json" if source.is_dir() else source
    if not path.exists():
        raise typer.BadParameter(f"{path} not found; run `pdf2xml convert` first")
    doc = Document.model_validate_json(path.read_text(encoding="utf-8"))
    targets = _targets(to)
    if not set(targets) <= {"bits", "jats"}:
        raise typer.BadParameter("export writes bits and/or jats only")
    cfg = Config(targets=targets, profile=profile, metadata=meta)  # type: ignore[arg-type]
    exports = publish(doc, path.parent, cfg)
    _print_exports(exports)
    report = path.parent / "report.json"
    if report.exists():
        data = _json.loads(report.read_text(encoding="utf-8"))
        data.setdefault("exports", {}).update(export_summary(exports))
        report.write_text(_json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
                          encoding="utf-8")
    raise typer.Exit(code=0 if all(r.valid for r in exports.values()) else 1)


@app.command()
def validate(
    files: Annotated[list[Path], typer.Argument(exists=True, dir_okay=False, help="XML file(s)")],
    schema: Annotated[str | None, typer.Option(
        help="bits | jats-archiving | jats-publishing | jats-authoring (default: detect)")] = None,
) -> None:
    """Validate BITS/JATS files against the bundled NLM DTDs (offline)."""
    from pdf2xml.publishing.schemas import SCHEMAS, validate_file

    if schema and schema not in SCHEMAS:
        raise typer.BadParameter(f"unknown schema {schema}; choose from {', '.join(SCHEMAS)}")
    bad = 0
    for f in files:
        key, errors = validate_file(f, schema)
        if errors:
            bad += 1
            typer.secho(f"✗ {f.name} [{key}] {len(errors)} error(s)", fg=typer.colors.RED)
            for e in errors[:20]:
                typer.echo(f"  {e}")
        else:
            typer.secho(f"✓ {f.name} [{key}] valid", fg=typer.colors.GREEN)
    raise typer.Exit(code=1 if bad else 0)


@app.command()
def inspect(
    pdf: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    out: Annotated[Path, typer.Option("--out", "-o")] = Path("out"),
    engine: Annotated[str, typer.Option()] = "auto",
    pages: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Triage + overlays only: see what the pipeline detects, without exporting."""
    from pdf2xml.pipeline import convert as run

    _setup_logging(False)
    cfg = Config(engine=engine, targets=["json"], pages=parse_pages(pages),  # type: ignore[arg-type]
                 overlays=True)
    res = run(pdf, out, cfg)
    typer.echo(json.dumps(res.triage.model_dump(), indent=2))
    typer.echo(f"overlays: {res.out_dir / 'qa'}")


@app.command()
def serve(
    root: Annotated[Path, typer.Argument(
        exists=True, file_okay=False, help="Output root with conversion folders")] = Path("out"),
    xslt: Annotated[list[Path] | None, typer.Option(
        "--xslt", exists=True,
        help="Your own XSLT (1.0–3.0) file or folder of .xsl files; repeatable. Files in "
             "<root>/_xslt/ are picked up too")] = None,
    port: Annotated[int, typer.Option(help="Port (0 = any free port)")] = 8765,
    host: Annotated[str, typer.Option(help="Interface to listen on")] = "127.0.0.1",
    no_browser: Annotated[bool, typer.Option("--no-browser", help="Don't open a browser")] = False,
    convert: Annotated[bool, typer.Option(
        help="Accept PDF uploads and convert them (POST /api/convert, page /upload)")] = True,
    engine: Annotated[str, typer.Option(
        help="Default engine for uploads: docling | heuristic | auto")] = "docling",
    max_upload_mb: Annotated[int, typer.Option(help="Largest PDF accepted, in MB")] = 300,
    cors_origin: Annotated[list[str] | None, typer.Option(
        "--cors-origin", help="Origin of a frontend served elsewhere that may call the API, "
                              "e.g. http://localhost:5173; repeatable")] = None,
) -> None:
    """Live preview: render BITS/JATS/canonical XML through XSLT, validate and check it; and
    convert PDFs uploaded from a web page.

    The page reloads when the XML, the stylesheet or a CSS file next to it changes.
    """
    from pdf2xml.jobs import ENGINES
    from pdf2xml.preview import serve as run

    if engine not in ENGINES:
        raise typer.BadParameter(f"engine must be one of {', '.join(ENGINES)}")
    run(root, host=host, port=port, sheets=list(xslt or []), open_browser=not no_browser,
        convert=convert, engine=engine, max_upload_mb=max_upload_mb,
        cors=list(cors_origin or []))


@app.command()
def schema(
    out: Annotated[Path, typer.Option("--out", "-o")] = Path(
        "src/pdf2xml/resources/schemas/canonical.schema.json"
    ),
) -> None:
    """Write the canonical JSON Schema (generated from the Pydantic models)."""
    from pdf2xml.model import Document

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(Document.model_json_schema(), indent=2), encoding="utf-8")
    typer.echo(f"wrote {out}")


if __name__ == "__main__":
    app()
