"""Legacy FastAPI interface backed by the updated conversion pipeline."""

from __future__ import annotations

import multiprocessing
import os
import shutil
import tempfile
import uuid
from collections.abc import AsyncIterator
from concurrent.futures import ProcessPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

import uvicorn
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from pdf2xml import __version__
from pdf2xml.config import Config, parse_pages
from pdf2xml.extract.structure_docling import configure_offline
from pdf2xml.pipeline import ConversionError
from pdf2xml.pipeline import convert as run_pipeline

OUTPUT_ROOT = Path(os.environ.get("PDF2XML_DATA") or os.environ.get("PDF2XML_OUT") or "out")
MAX_UPLOAD_BYTES = int(os.environ.get("PDF2XML_MAX_UPLOAD_MB", "300")) * 1024 * 1024
XML_OUTPUTS = {
    "jats": ("jats", "41_jats.xml"),
    "bits": ("bits", "40_bits.xml"),
    "rawxml": ("xml", "30_canonical.xml"),
}


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    configure_offline()
    # One persistent child keeps ML models warm and Saxon on the thread that created it.
    with ProcessPoolExecutor(
        max_workers=1, mp_context=multiprocessing.get_context("spawn")
    ) as worker:
        application.state.conversions = worker
        yield


app = FastAPI(
    title="pdf2xml API",
    description="FastAPI wrapper for the pdf2xml conversion pipeline.",
    version=__version__,
    lifespan=lifespan,
)
cors_origins = [s.strip() for s in os.environ.get("PDF2XML_CORS_ORIGINS", "").split(",")
                if s.strip()]
if cors_origins:
    app.add_middleware(
        CORSMiddleware, allow_origins=cors_origins,
        allow_methods=["GET", "POST"], allow_headers=["Content-Type"],
    )


@app.get("/")
def read_root() -> dict[str, str]:
    return {"message": "pdf2xml API is running. Use POST /convert to convert PDFs."}


def _run_conversion(pdf: Path, out: Path, cfg: Config) -> dict[str, Any]:
    """Return only serializable response metadata from the conversion process."""
    res = run_pipeline(pdf, out, cfg)
    return {
        "output_directory": str(res.out_dir.absolute()),
        "blocks": len(res.document.body),
        "coverage": res.coverage.ratio,
        "xml_valid": not res.xml_errors,
    }


@app.post("/convert", response_model=None)
def convert_pdf(
    background_tasks: BackgroundTasks,
    file: Annotated[UploadFile, File()],
    engine: str = "docling",
    targets: str = "json,xml,epub,docx",
    return_xml: str | None = None,
    pages: str | None = None,
) -> FileResponse | JSONResponse:
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")
    target_list = [t.strip() for t in targets.split(",") if t.strip()]
    try:
        selected_pages = parse_pages(pages)
        if not target_list or (selected_pages is not None and not selected_pages):
            raise ValueError("targets and page selections must not be empty")
        if selected_pages and min(selected_pages) < 1:
            raise ValueError("pages must use positive, 1-based page numbers")
        cfg = Config(engine=engine, targets=target_list, pages=selected_pages,
                     overlays=False, reuse_cache=False)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if return_xml:
        if return_xml not in XML_OUTPUTS:
            raise HTTPException(status_code=400,
                                detail="return_xml must be 'jats', 'bits', or 'rawxml'")
        available = set(target_list)
        if "epub" in available:
            available.add("xml")  # EPUB generation also writes canonical XML, as before.
        if XML_OUTPUTS[return_xml][0] not in available:
            raise HTTPException(
                status_code=404,
                detail=f"{return_xml} XML not found. Ensure it is included in targets.",
            )

    job_output_dir = OUTPUT_ROOT / str(uuid.uuid4())
    temp_pdf_path: Path | None = None
    try:
        job_output_dir.mkdir(parents=True, exist_ok=True)
        # Keep the input beside its request's output so every failure can clean up both.
        with tempfile.NamedTemporaryFile(dir=job_output_dir, suffix=".pdf", delete=False) as tmp:
            temp_pdf_path = Path(tmp.name)
            size = 0
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="PDF exceeds the upload size limit")
                tmp.write(chunk)
        result = app.state.conversions.submit(
            _run_conversion, temp_pdf_path, job_output_dir, cfg
        ).result()
        if return_xml:
            xml_path = Path(result["output_directory"]) / XML_OUTPUTS[return_xml][1]
            if not xml_path.is_file():
                raise HTTPException(
                    status_code=404,
                    detail=f"{return_xml} XML not found. Ensure it is included in targets.",
                )
            response = FileResponse(path=xml_path, media_type="application/xml",
                                    filename=xml_path.name)
        else:
            response = JSONResponse(content={
                "status": "success", "filename": file.filename, **result,
            })
        background_tasks.add_task(shutil.rmtree, job_output_dir, ignore_errors=True)
        return response
    except HTTPException:
        shutil.rmtree(job_output_dir, ignore_errors=True)
        raise
    except ConversionError as e:
        shutil.rmtree(job_output_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(e)) from e
    except Exception as e:
        shutil.rmtree(job_output_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"Unexpected error: {e}") from e
    finally:
        if temp_pdf_path is not None:
            temp_pdf_path.unlink(missing_ok=True)


if __name__ == "__main__":
    uvicorn.run("api:app", host="0.0.0.0", port=int(os.environ.get("PDF2XML_PORT", "8087")))
