# pdf2xml: deploy edition

Converts born-digital PDF files to XML in one pass:

```
PDF → Docling layout analysis → canonical JSON → canonical XML → BITS 2.2 → JATS 1.4
```

Everything it needs is inside this folder:

- its own Python;
- every library;
- the Docling models.

It does not install anything, needs no internet connection and never downloads a model.

The package does only this conversion. EPUB, DOCX, GROBID, the preview server and the QA reports are not included.

## 1. Requirements

| | |
|---|---|
| System | Windows 10 or 11, 64-bit |
| Disk | About 2.5 GB for the extracted folder, plus space for the output |
| Memory | 8 GB RAM or more recommended (a 300-page book uses about 3–4 GB) |
| Software | Nothing else: no Python, no internet. |

Scanned PDFs (pages that are only images) are rejected, because OCR is not part of this tool.

## 2. Install

1. Copy the zip to the machine.
2. Extract it to a **short path**, for example `C:\pdf2xml`. Some library files have long names, and a deep folder can exceed the Windows 260-character path limit.
3. That is all. The folder looks like this:

```
C:\pdf2xml\
  convert.bat         ← run this
  README.md           ← this file
  python\             ← private Python with all libraries (do not change)
  models\             ← Docling layout and table models (do not change)
  examples\           ← sample export profiles and metadata files
  output\             ← created on the first run; results go here
  THIRD-PARTY.txt     ← bundled libraries and their licences
  VERSION.txt, MANIFEST.txt
```

To move the tool, move the whole folder. Nothing is stored anywhere else.

## 3. Run

There are three ways to start a conversion.

**Drag and drop:** drop one or more PDF files, or a folder of PDFs, onto `convert.bat`. The results go to `C:\pdf2xml\output\`.

**From a command prompt** (`cmd`):

```bat
cd C:\pdf2xml
convert.bat D:\books\book.pdf
convert.bat D:\books\book.pdf D:\books\chapter2.pdf -o D:\xml-out
convert.bat D:\books              :: every PDF in the folder
```

**From PowerShell:**

```powershell
C:\pdf2xml\convert.bat D:\books\book.pdf -o D:\xml-out
```

Each PDF prints one line:

```
OK     book.pdf → C:\pdf2xml\output\book  (coverage=1.0000 xml_valid=True bits_valid=True jats_valid=True, 412 s)
```

- `OK`: every word was kept and all XML files are schema-valid.
- `CHECK`: look at `report.json` in that PDF's folder (section 5).
- `FAILED`: the PDF could not be converted, for example because it is encrypted or scanned. The other PDFs still run.

### Options

| Option | Meaning |
|---|---|
| `-o <folder>`, `--out` | Output folder (default: `output\` inside the tool folder). Each PDF gets `<folder>\<pdf name>\` |
| `--to json,xml,bits,jats` | Which outputs to write (default: all four). JSON is always written, because the others are made from it |
| `--pages "1-20,25"` | Convert only these pages |
| `--profile <file.yaml>` | BITS/JATS house rules (see `examples\profiles\`) |
| `--meta <file.yaml>` | Book, journal or chapter metadata: ISBN, DOI, authors, dates … (see `examples\metadata\`) |
| `--no-cache` | Run the layout analysis again. Normally, converting the same unchanged PDF again reuses it, which takes seconds instead of minutes |
| `-v` | Detailed log |
| `--help`, `--version` | Help, version |

The exit code is `0` when every PDF converted and `1` when one failed, so the tool can run in scripts.

## 4. Output

Each PDF gets its own folder, `output\<pdf name>\`:

| File | Contents |
|---|---|
| `20_canonical.json` | **Canonical JSON**: every block (headings, paragraphs, lists, tables, figures, footnotes …) with its text, styles (font, size, bold, italic, colour), position and reading order |
| `30_canonical.xml` | **Canonical XML** (S4C XML): the same document as XML, validated against its RelaxNG schema |
| `40_bits.xml` | **BITS 2.2**: a whole book (`<book>`: front matter, chapters, back matter, index) or one chapter (`<book-part-wrapper>`), validated against the official NLM DTD |
| `41_jats.xml` | **JATS 1.4** (Archiving tag set): the document as one `<article>`, validated against the official NLM DTD |
| `assets\` | Figure images cut from the pages; the XML files link to them |
| `report.json` | Summary: text coverage, schema validity and errors, warnings, statistics, timings |
| `00_triage.json`, `10_style_layer.json`, `11_structure_raw.json` | Intermediate stages. `11_structure_raw.json` is the saved layout analysis that a re-run reuses. `10_style_layer.json` can be large (tens of MB for a book) |

To use the XML in another system, take `40_bits.xml` or `41_jats.xml` together with the `assets\` folder.

## 5. Checking a result

Open `report.json`:

- `"coverage": {"ok": true, "ratio": 1.0}`: every character of the PDF's text is in the output.
- `"xml_valid": true`: the canonical XML matches its schema.
- `"exports" → "bits" / "jats" → "valid": true`: the BITS/JATS file matches the official DTD. If it is `false`, `"errors"` lists the reasons, with line numbers in the XML file.
- `"warnings"`: things worth a look. For example, references that could not be split into parts, or citations without a matching reference.

## 6. Profiles and metadata

The PDF does not say everything a publishing system needs, such as a DOI or a journal ISSN. Put that in a metadata file. House rules (ID patterns, JATS tag set, page markers …) go in a profile. Copy a sample from `examples\` and edit it:

```bat
convert.bat D:\books\ch11.pdf --profile examples\profiles\bits-chapter.yaml --meta examples\metadata\chapter.yaml
```

Without them, the defaults give standard, valid BITS 2.2 and JATS 1.4 Archiving.

## 7. Troubleshooting

| Problem | What to do |
|---|---|
| Nothing happens, or a "path too long" error | Extract to a short folder such as `C:\pdf2xml` |
| `no usable text layer` | The PDF is scanned (images only). OCR is not supported |
| `encrypted PDF` | Remove the password protection first |
| Very slow on the first PDF | Loading the models takes 30–60 s once per run. Then a book takes a few minutes, depending on the page count and the CPU. Converting many PDFs in one command loads the models only once |
| The antivirus blocks `python.exe` | Allow the `C:\pdf2xml\python` folder. It is a standard CPython from python.org |
| `models folder …: missing …` | The `models` folder was changed or not fully extracted. Extract the zip again |

## 8. What is inside

- Python 3.13 (CPython for Windows x64) with the Visual C++ runtime DLLs.
- Docling (IBM, MIT licence) with its layout ("heron") and TableFormer models (`models\`).
- PyTorch for CPU. No GPU is needed, and none is used.
- pdfplumber, pypdfium2, pikepdf, lxml, Pydantic, Pillow, PyYAML.
- The official NLM DTDs for BITS 2.2 and JATS 1.4 (offline validation).

`THIRD-PARTY.txt` lists every bundled library with its version and licence. `VERSION.txt` gives the tool version and the build date.
