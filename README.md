# pdf2xml

Convert born-digital PDFs into a canonical JSON/XML document, then into EPUB 3, DOCX and the NISO publishing standards **BITS 2.2** (books and chapters) and **JATS 1.4** (journal articles), keeping both the **structure** (headings, lists, tables, figures, captions, footnotes, reading order) and the **styles** (fonts, sizes, bold/italic, colours, paragraph geometry).

Scanned PDFs are not supported: the PDF must have a text layer. OCR is out of scope.

Frontend developers: start with [docs/HANDOVER-FRONTEND.md](docs/HANDOVER-FRONTEND.md) (HTTP API, output files, data formats, open points).

## Requirements

- Python **3.11–3.13** (3.14 is not supported yet)
- Java, only for the EPUB validation test (EPUBCheck)

## Installation

```powershell
py -3.13 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e .              # core
pip install -e ".[ml]"        # optional: Docling layout/table models (large; pulls torch)
pip install --group dev       # optional: test and lint tools (pip 25.1+)
```

With [uv](https://docs.astral.sh/uv/): `uv sync` (add `--extra ml` for Docling), then prefix commands with `uv run`.

## Usage

```powershell
pdf2xml convert report.pdf -o out
pdf2xml convert a.pdf b.pdf --engine heuristic --to json,xml --pages "1-3,7"
pdf2xml convert ch11.pdf --to bits --profile house.yaml --meta ch11.yaml
pdf2xml export out/ch11 --to bits,jats --profile other.yaml   # re-export without re-reading the PDF
pdf2xml validate out/ch11/40_bits.xml supplier.xml              # check any BITS/JATS file, offline
pdf2xml inspect report.pdf         # triage + QA overlays only, no export
pdf2xml schema                     # regenerate the canonical JSON Schema
```

| Option | Default | Meaning |
|---|---|---|
| `--out`, `-o` | `out` | Output root; each PDF gets `out/<pdf-name>/` |
| `--to` | `json,xml,epub,docx,bits,jats` | Output formats to write |
| `--profile` | built-in defaults | BITS/JATS export profile (YAML or JSON) |
| `--meta` | none | Book/journal/chapter metadata (YAML or JSON) |
| `--engine` | `auto` | `docling`, `heuristic`, or `auto` (Docling if installed) |
| `--pages` | all | Page selection, e.g. `"1-3,7"` |
| `--overlays / --no-overlays` | on | Write QA overlay PNGs |
| `--no-cache` | off | Re-run Docling instead of reusing its cached output |
| `--verbose`, `-v` | off | Debug logging |

### Structure engines

- **Docling** (`pip install -e ".[ml]"`): ML layout analysis with table structure. Best quality. The models take about 40 s to load; results are cached per PDF, so re-running on an unchanged file is fast.
- **Heuristic**: rule-based, fast, no ML. Good for simple single- and two-column documents. It does **not** detect tables (cells come out as paragraphs).

## Output

Each PDF produces a folder of numbered files, one per pipeline stage:

| File | Contents |
|---|---|
| `00_triage.json` | Page count, encryption, tagged PDF, text layer, document-type guess |
| `10_style_layer.json` | Every word with font, size, colour, weight and position |
| `11_structure_raw.json` | Blocks and reading order from the structure engine |
| `20_canonical.json` | The canonical document (the source for all exports) |
| `30_canonical.xml` | The same document as XML, validated against `canonical.rng` |
| `40_bits.xml` | BITS 2.2 (`book-part-wrapper` or `book`), validated against the NLM DTD |
| `41_jats.xml` | JATS 1.4 `article` (Archiving, Publishing or Authoring tag set), validated against the NLM DTD |
| `50_book.epub` | Reflowable EPUB 3 with a generated table of contents and CSS |
| `51_document.docx` | Word document with named styles (Normal, Heading 1…, Caption, …) |
| `assets/` | Figure images cropped from the PDF |
| `qa/` | Page images with detected blocks drawn on top, for visual checking |
| `report.json` | Text coverage, XML validity, warnings, statistics and timings |

The CLI prints a one-line summary per PDF. If it says `check report.json`, either some text was not accounted for (`coverage`) or the XML failed schema validation.

### Canonical format

- Coordinates are PDF points with a top-left origin: `[x0, y0, x1, y1]`.
- Styles are collected into a catalogue (`body`, `h1`…`h6`, `caption`, `code`, …). Each block points to a style, and inline runs store only what differs from it (e.g. a bold word in a body paragraph).
- Running headers, footers, page numbers and text inside figures are kept separately as `artifacts`, not in the body.
- Schemas: `src/pdf2xml/resources/schemas/canonical.rng` (XML) and `canonical.schema.json` (JSON).

## BITS and JATS

The publishing exports add the document structure the standards need on top of the canonical document:

- the title, contributors, abstract and keywords, moved into the metadata;
- nested `sec` elements, with levels ranked from the heading styles;
- `table-wrap` and `fig` with split labels ("Table 11.1" + title) and "Source:" notes;
- boxes (`boxed-text`), epigraphs (`disp-quote`) and a reference list (`ref-list/ref/mixed-citation`);
- links for citations, "Table/Figure/Box" callouts and URLs/DOIs (`xref`, `ext-link`).

Proof-stage author queries ("AQ: …") are separated from the text, and paragraphs split by column or page breaks are rejoined.

Every file is validated against the official NLM DTDs, which are bundled in `src/pdf2xml/resources/schemas/nlm/` so validation works offline. The result is in `report.json` under `exports`.

### Profiles and metadata

A **profile** holds a client's house rules; see `examples/profiles/`:

| Setting | Values (default first) |
|---|---|
| `bits.root` | `book-part-wrapper` (one chapter) · `book` |
| `bits.version` / `jats.version` | `2.2` / `1.4`, or an older value for `@dtd-version` |
| `jats.tag_set` | `archiving` · `publishing` (needs journal id + ISSN) · `authoring` (needs contributors + abstract) |
| `bits.doctype` / `jats.doctype` | `official` · `none` · a full custom `<!DOCTYPE …>` line |
| `sections.mode` | `auto` (levels from heading styles) · `style-map` (catalogue style → level) |
| `sections.disp_level` | off · a pattern such as `level{n}` |
| `ids` | patterns per element, e.g. `sec: "ch{chapter}lev{level}sec{n}"`, `table: "tab{chapter}_{n}"` |
| `citation_links` | `auto` · `author-year` · `numeric` · `none` |
| `callout_links`, `ext_links` | on · off |
| `styling` | `semantic` · `keep-visual` (colours as `styled-content`) |
| `queries.mode` | `drop` · `comment` · `pi` · `keep` |

**Metadata** (book, journal, chapter/article) comes from a sidecar file and overrides anything detected in the PDF. See `examples/metadata/`.

JATS defaults to the **Archiving** tag set because it is the only one whose front matter is fully optional. Choose `publishing` or `authoring` in the profile when you have the metadata those tag sets require. If it is missing, the export is marked invalid and `report.json` says which field to add.

## Development

```powershell
pytest -m "not slow"                  # fast suite (heuristic engine)
pytest -m slow                        # Docling end-to-end tests
pytest --update-goldens               # rewrite expected outputs after an intended change; review the diff
ruff check src tests
mypy src
python tests/fixtures/make_fixtures.py   # regenerate the test PDFs (needs reportlab)
```

Test PDFs live in `tests/fixtures/pdfs/` and their expected canonical JSON in `tests/fixtures/expected/`. The integration tests also check that every character of the PDF text appears exactly once in the output, that the XML is schema-valid, that the EPUB passes EPUBCheck, and that output is byte-identical across runs.

## Current limitations

- No OCR; scanned or image-only PDFs are rejected.
- Encrypted PDFs that need a password are rejected.
- Tagged-PDF structure trees are detected but not used yet.
- Formulas are exported as text (no MathML/OMML yet).
- DOCX lists use literal markers rather than Word numbering; hyperlinks are plain text.
- Fixed-layout EPUB (for magazines) is not implemented; `--epub-layout fixed` falls back to reflowable.
- BITS/JATS: references are `mixed-citation` (printed text with punctuation); structured `element-citation` needs a reference parser and is not built. Boxes are recognised only from headings labelled "Box N". Formulas are exported as TeX when the engine provides it, otherwise as text.

## Licence

Proprietary. Third-party dependencies are permissively licensed (MIT, BSD, Apache-2.0, MPL-2.0); see `pyproject.toml`.
