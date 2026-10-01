# pdf2xml

Convert born-digital PDFs into a canonical JSON/XML document, then into EPUB 3, DOCX and the NISO publishing standards **BITS 2.2** and **JATS 1.4**. 

This project includes a robust FastAPI server allowing you to run these conversions via an HTTP API.

## Requirements

- Python **3.11, 3.12, or 3.13** (Note: Python 3.14 is not supported yet)

## Installation

### 1. Create and Activate a Virtual Environment

Ensure you are using a supported Python version when creating your virtual environment:

```bash
python3.13 -m venv .venv
source .venv/bin/activate
```

### 2. Install Dependencies

Install the project along with its core dependencies (including FastAPI and Uvicorn):

```bash
pip install -e .
```

*Optional: If you wish to use the high-quality machine learning layout extraction models (Docling), install the `ml` extra (this may take a bit longer):*
```bash
pip install -e ".[ml]"
```

## Running the API Server

The project includes a standalone FastAPI application (`api.py`) for PDF conversions.

1. Start the server on port 8080 (or your preferred port):
```bash
.venv/bin/uvicorn api:app --reload --port 8080
```

2. Once running, explore the interactive API documentation and test file uploads directly from your browser: 
[http://localhost:8080/docs](http://localhost:8080/docs)

### API Usage Example (cURL)

Convert a PDF and return the raw JATS XML output directly in the response:

```bash
curl -X 'POST' \
  'http://localhost:8080/convert?engine=heuristic&targets=json,xml,jats,bits&return_xml=jats' \
  -H 'accept: application/xml' \
  -F 'file=@/path/to/your/document.pdf'
```

## Command Line Interface (CLI)

You can also use the original built-in command-line tools provided by `pdf2xml`:

```bash
pdf2xml convert my_document.pdf -o out --engine heuristic
```

## How It Works

The core logic of `pdf2xml` relies on a highly accurate two-layer extraction pipeline that ensures **no text is ever dropped** during the conversion:

1. **Style Layer (`pdfplumber`)**: 
   The pipeline extracts every single word from the PDF, recording its exact font, size, color, bold/italic weight, and physical bounding box (coordinates). It knows *how text looks*.
2. **Structure Layer (`docling` or `heuristic`)**: 
   A separate engine extracts the geometrical blocks (headings, paragraphs, lists, tables) and determines the overall reading order of the document. It knows *what the blocks are*.
3. **Canonical Merge**:
   The two layers are merged together by geometry. Each word is assigned to the structural block covering the largest share of its area. If a word falls outside a block, it is attached to a nearby block or inserted into the reading order—guaranteeing 100% text coverage.
4. **Enrichment and Export**:
   The merged data becomes the **Canonical JSON/XML Document**. This flat canonical document is then parsed to build hierarchical semantic structures (like `book`, `chapter`, `sec`, references, and footnotes). Finally, it's serialized into the desired target standards like **BITS 2.2** or **JATS 1.4**, passing through strict NLM DTD schema validation.
