"""Legacy FastAPI interface backed by the updated conversion pipeline."""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import zipfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

import uvicorn
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from pdf2xml import __version__
from pdf2xml.config import Config, parse_pages
from pdf2xml.extract.structure_docling import configure_offline
from pdf2xml.jobs import ConversionQueue, JobError, OUTPUT_FILES
from pdf2xml.security import authenticate, credentials
from pdf2xml.observability import configure_logging
from pdf2xml.http_limits import RequestLimits

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
    force=True,
)
log = logging.getLogger("pdf2xml.api")

OUTPUT_ROOT = Path(os.environ.get("PDF2XML_DATA") or os.environ.get("PDF2XML_OUT") or "out")
MAX_UPLOAD_BYTES = int(os.environ.get("PDF2XML_MAX_UPLOAD_MB", "300")) * 1024 * 1024
XML_OUTPUTS = {
    "jats": ("jats", "41_jats.xml"),
    "bits": ("bits", "40_bits.xml"),
    "rawxml": ("xml", "30_canonical.xml"),
    "content": ("content", "42_content.xml"),
}


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    credentials()
    configure_logging()
    configure_offline()
    log.info("pdf2xml %s: conversion error logging enabled; data=%s; models=%s",
             __version__, OUTPUT_ROOT,
             os.environ.get("PDF2XML_MODELS_DIR", "project models/ or local cache"))
    application.state.jobs = ConversionQueue(
        OUTPUT_ROOT, max_upload_mb=MAX_UPLOAD_BYTES // (1024 * 1024),
        views=False, targets=("json", "xml", "content", "bits", "jats", "epub", "docx"),
        max_pending=int(os.environ.get("PDF2XML_MAX_PENDING", "32")),
        max_runtime_s=float(os.environ.get("PDF2XML_JOB_TIMEOUT_S", "1800")),
        retention_s=float(os.environ.get("PDF2XML_RETENTION_HOURS", "24")) * 3600,
        max_pages=int(os.environ.get("PDF2XML_MAX_PAGES", "1000")),
    )
    try:
        yield
    finally:
        application.state.jobs.close()


app = FastAPI(
    title="pdf2xml API",
    description="FastAPI wrapper for the pdf2xml conversion pipeline.",
    version=__version__,
    lifespan=lifespan,
)
app.add_middleware(RequestLimits, max_bytes=MAX_UPLOAD_BYTES + 1024 * 1024, require_auth=True)
cors_origins = [s.strip() for s in os.environ.get("PDF2XML_CORS_ORIGINS", "").split(",")
                if s.strip()]
if cors_origins:
    app.add_middleware(
        CORSMiddleware, allow_origins=cors_origins,
        allow_methods=["GET", "POST", "DELETE"], allow_headers=["Content-Type", "Authorization"],
        expose_headers=["X-PDF2XML-Quality", "X-PDF2XML-Issue-Count"],
    )


@app.get("/")
def read_root(owner: str = Depends(authenticate)) -> dict[str, str]:
    return {"message": "pdf2xml API is running. Use POST /convert to convert PDFs."}


@app.get("/health/live")
def live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
def ready() -> dict[str, str]:
    if not hasattr(app.state, "jobs") or app.state.jobs._closed:
        raise HTTPException(503, "Job service unavailable")
    if not app.state.jobs._listener.is_alive():
        raise HTTPException(503, "Job listener unavailable")
    from importlib.util import find_spec
    from pdf2xml.extract.structure_docling import resolve_models_dir

    if find_spec("docling") is None:
        raise HTTPException(503, "Docling is not installed")
    try:
        resolve_models_dir()
    except ValueError:
        raise HTTPException(503, "Local model files are unavailable") from None
    return {"status": "ready"}


@app.get("/metrics")
def metrics(owner: str = Depends(authenticate)) -> dict[str, Any]:
    return app.state.jobs.metrics(owner)


@app.get("/metrics/prometheus", response_class=PlainTextResponse)
def prometheus_metrics(owner: str = Depends(authenticate)) -> PlainTextResponse:
    data = app.state.jobs.metrics(owner)
    gauges = {"queue_capacity": data["capacity"], "listener_alive": int(data["listener_alive"]),
              "recent_completed": data["recent_completed"], "recent_failures": data["recent_failures"],
              "oldest_queue_wait_seconds": data["oldest_queue_wait_s"], "needs_review": data["needs_review"]}
    gauges.update({f"jobs_{state}": value for state, value in data["states"].items()})
    return PlainTextResponse("".join(f"# TYPE pdf2xml_{name} gauge\npdf2xml_{name} {value}\n"
                                     for name, value in gauges.items()),
                             media_type="text/plain; version=0.0.4")


@app.post("/jobs", status_code=202)
def submit_job(file: Annotated[UploadFile, File()],
               request: Request,
               engine: str = "docling",
               targets: str = "json,xml", pages: str | None = None,
               table_mode: str = "accurate",
               owner: str = Depends(authenticate)) -> dict[str, Any]:
    client_code = request.headers.get("x-client-code", "").strip()
    project_name = request.headers.get("x-project-name", "").strip()
    project_id = request.headers.get("x-project-id", "").strip()
    project_tag = f"[{client_code}/{project_name}#{project_id}]" if (project_name or project_id) else ""
    log.info("%s Incoming job submission: file='%s' | engine=%s | targets=%s",
             project_tag, file.filename, engine, targets)
    # Bound the read even when the caller omits Content-Length.
    data = file.file.read(MAX_UPLOAD_BYTES + 1)
    try:
        job = app.state.jobs.submit(data, file.filename or "document.pdf", {
            "engine": engine, "to": targets, "pages": pages or "", "overlays": "false", "table_mode": table_mode,
        }, owner=owner)
        log.info("[%s]%s Queued job %s: file='%s'", job.id[:8], project_tag, job.id, file.filename)
        return _job_response(job)
    except JobError as e:
        raise HTTPException(e.status, detail={"code": e.code, "message": e.message}) from e


def _job_response(job: Any) -> dict[str, Any]:
    data = job.to_json()
    data["links"] = {"self": f"/jobs/{job.id}", "artifacts": f"/jobs/{job.id}/artifacts",
                     "report": f"/jobs/{job.id}/report"}
    # Preview routes are not served by this API.
    data.pop("result", None)
    return data


def _get_job(job_id: str, owner: str) -> Any:
    try:
        job = app.state.jobs.get(job_id)
        if job.owner != owner:
            raise HTTPException(404, "Job not found")
        return job
    except JobError as e:
        raise HTTPException(e.status, e.message) from e


@app.get("/jobs/{job_id}")
def get_job(job_id: str, owner: str = Depends(authenticate)) -> dict[str, Any]:
    data = _job_response(_get_job(job_id, owner))
    job = _get_job(job_id, owner)
    if job.result:
        data["quality"] = job.result.get("quality")
    return data


@app.delete("/jobs/{job_id}")
def cancel_job(job_id: str, owner: str = Depends(authenticate)) -> dict[str, Any]:
    _get_job(job_id, owner)
    try:
        return _job_response(app.state.jobs.cancel(job_id))
    except JobError as e:
        raise HTTPException(e.status, e.message) from e


@app.get("/jobs/{job_id}/artifacts")
def list_artifacts(job_id: str, owner: str = Depends(authenticate)) -> dict[str, str]:
    job = _get_job(job_id, owner)
    if job.state not in ("done", "failed"):
        raise HTTPException(409, "Conversion has not completed")
    folder = OUTPUT_ROOT.resolve() / job.doc
    reports = {"quality", "quality_html", "report", "validation_canonical", "validation_bits",
               "validation_jats", "validation_content", "completeness"}
    return {name: f"/jobs/{job_id}/artifacts/{name}" for name, (filename, _) in OUTPUT_FILES.items()
            if (folder / filename).is_file() and (job.state == "done" or name in reports)}


@app.get("/jobs/{job_id}/report")
def quality_report(job_id: str, owner: str = Depends(authenticate)) -> dict[str, Any]:
    job = _get_job(job_id, owner)
    if job.state not in ("done", "failed"):
        raise HTTPException(409, "Report is not available yet")
    path = OUTPUT_ROOT.resolve() / job.doc / "quality.json"
    if not path.is_file():
        from pdf2xml.qa.report import failure_report
        return failure_report(job.filename, job.error or "Report could not be generated", job.stage,
                              job.options.get("to", []))
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/jobs/{job_id}/artifacts/{target}")
def download_artifact(job_id: str, target: str, owner: str = Depends(authenticate)) -> FileResponse:
    artifacts = list_artifacts(job_id, owner)
    if target not in artifacts:
        raise HTTPException(404, "Artifact not found")
    path = OUTPUT_ROOT.resolve() / _get_job(job_id, owner).doc / OUTPUT_FILES[target][0]
    return FileResponse(path, filename=path.name)


def _zip_xml_outputs(job_dir: Path, out_dir: Path, selections: list[str]) -> FileResponse:
    """Bundle every XML output of one conversion into a single ZIP download."""
    zip_path = job_dir / "pdf2xml-xml.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for selection in selections:
            _, filename = XML_OUTPUTS[selection]
            xml_path = out_dir / filename
            if not xml_path.is_file():
                raise HTTPException(
                    status_code=404,
                    detail=f"{filename} not found. Ensure its target is included in targets.",
                )
            archive.write(xml_path, arcname=filename)
    return FileResponse(path=zip_path, media_type="application/zip", filename=zip_path.name)


@app.post("/convert", response_model=None)
def convert_pdf(
    file: Annotated[UploadFile, File()],
    request: Request,
    engine: str = "docling",
    targets: str = "json,xml",
    return_xml: str | None = None,
    pages: str | None = None,
    table_mode: str = "accurate",
    owner: str = Depends(authenticate),
) -> FileResponse | JSONResponse:
    start_time = time.monotonic()
    client_code = request.headers.get("x-client-code", "").strip()
    project_name = request.headers.get("x-project-name", "").strip()
    project_id = request.headers.get("x-project-id", "").strip()
    project_tag = f"[{client_code}/{project_name}#{project_id}]" if (project_name or project_id) else ""

    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")
    log.info("%s Incoming convert request: file='%s' | engine=%s | targets=%s | return_xml=%s",
             project_tag, file.filename, engine, targets, return_xml)
    target_list = [t.strip() for t in targets.split(",") if t.strip()]
    try:
        selected_pages = parse_pages(pages)
        if not target_list or (selected_pages is not None and not selected_pages):
            raise ValueError("targets and page selections must not be empty")
        if selected_pages and min(selected_pages) < 1:
            raise ValueError("pages must use positive, 1-based page numbers")
        cfg = Config.model_validate({"engine": engine, "targets": target_list, "table_mode": table_mode,
                                     "pages": selected_pages, "overlays": False,
                                     "reuse_cache": False})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if return_xml:
        if return_xml != "all" and return_xml not in XML_OUTPUTS:
            raise HTTPException(status_code=400,
                                detail="return_xml must be 'jats', 'bits', 'rawxml', "
                                       "'content', or 'all'")
        available = set(target_list)
        if "epub" in available:
            available.add("xml")  # EPUB generation also writes canonical XML, as before.
        # Preserve the original three-output ZIP contract; include the new optional
        # Content XML when its target is explicitly requested.
        wanted = ([k for k in XML_OUTPUTS if k != "content" or "content" in available]
                  if return_xml == "all" else [return_xml])
        missing = [name for name in wanted if XML_OUTPUTS[name][0] not in available]
        if missing:
            raise HTTPException(
                status_code=404,
                detail=f"{', '.join(missing)} XML not found. Ensure it is included in targets.",
            )

    try:
        job = app.state.jobs.submit(
            file.file.read(MAX_UPLOAD_BYTES + 1), file.filename,
            {"engine": engine, "to": ",".join(cfg.targets), "pages": pages or "",
             "overlays": "false", "table_mode": table_mode}, owner=owner,
        )
    except JobError as e:
        raise HTTPException(e.status, e.message) from e
    # Compatibility endpoint: new clients should use POST /jobs and poll instead.
    while job.state in ("queued", "running"):
        time.sleep(0.1)
    if job.state != "done":
        raise HTTPException(422 if job.state == "cancelled" else 500,
                            "Conversion did not complete; inspect the job status.")
    out_dir = OUTPUT_ROOT.resolve() / job.doc
    report = json.loads((out_dir / "report.json").read_text(encoding="utf-8"))
    quality = report["quality"]
    elapsed = round(time.monotonic() - start_time, 2)
    log.info("[%s]%s Completed convert: file='%s' in %.2fs (quality: %s, issues: %s)",
             job.id[:8], project_tag, file.filename, elapsed, quality.get("status"), quality.get("issue_count"))
    result = {
        "job_id": job.id, "artifacts": f"/jobs/{job.id}/artifacts",
        **({"output_directory": str(out_dir)} if owner == "local" else {}),
        "blocks": report["blocks"], "coverage": report["coverage"]["ratio"],
        "xml_valid": report["xml_valid"], "quality": quality,
    }
    response: FileResponse | JSONResponse
    if return_xml == "all":
        response = _zip_xml_outputs(out_dir, out_dir, wanted)
    elif return_xml:
        xml_path = out_dir / XML_OUTPUTS[return_xml][1]
        if not xml_path.is_file():
            raise HTTPException(404, "Requested XML was not generated")
        response = FileResponse(xml_path, media_type="application/xml", filename=xml_path.name)
    else:
        response = JSONResponse(content={
            "status": "needs_review" if quality["status"] == "needs_review" else "success",
            "filename": file.filename, **result,
        })
    response.headers["X-PDF2XML-Quality"] = quality["status"]
    response.headers["X-PDF2XML-Issue-Count"] = str(quality["issue_count"])
    return response


if __name__ == "__main__":
    uvicorn.run("api:app", host="0.0.0.0", port=int(os.environ.get("PDF2XML_PORT", "8087")))
