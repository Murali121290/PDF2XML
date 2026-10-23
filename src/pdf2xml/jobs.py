"""Conversion jobs: PDFs uploaded through the web API, converted one at a time.

The HTTP server answers many requests at once; conversions run in one child process, one after
another. That process keeps Docling's models loaded between jobs (loading them takes ~40 s),
keeps Saxon on a single thread (saxonche needs that), and can be stopped to cancel a running
job without taking the server down. It reports each pipeline stage as it starts, so the web
page can show where a long conversion is.

Uploaded PDFs are kept in ``<out>/_uploads/<job id>/``; the results go to ``<out>/<name>/``
exactly as ``pdf2xml convert`` writes them, so every other API route works on them unchanged.
Jobs live in memory: the list starts empty when the server starts (the conversion folders stay).
"""

from __future__ import annotations

import contextlib
import json
import multiprocessing
import queue
import re
import threading
import time
import unicodedata
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pdf2xml.config import ALL_TARGETS, Config, parse_pages

ENGINES = ("docling", "heuristic", "auto")
STATES = ("queued", "running", "done", "failed", "cancelled")
# pipeline stages in the order they run (``pipeline.convert`` reports them)
STAGES = ("style_layer", "structure_layer", "merge_build", "figures", "xml", "epub", "docx",
          "bits_jats", "overlays")


class JobError(Exception):
    """A request the queue refuses; ``status`` is the HTTP status, ``code`` a stable name."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass
class Job:
    id: str
    doc: str                            # output folder name: the PDF name without ".pdf"
    filename: str                       # the uploaded file name, as sent
    pdf: Path
    options: dict[str, Any]
    state: str = "queued"
    stage: str | None = None
    error: str | None = None
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    result: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        now = time.time()
        end = self.finished or now
        return {
            "id": self.id, "doc": self.doc, "filename": self.filename,
            "options": self.options, "state": self.state, "stage": self.stage,
            "stage_index": STAGES.index(self.stage) + 1 if self.stage in STAGES else 0,
            "stage_count": len(STAGES),
            "error": self.error,
            "created": _iso(self.created), "started": _iso(self.started),
            "finished": _iso(self.finished),
            "elapsed_s": round(end - self.started, 1) if self.started else 0.0,
            "result": self.result,
            "links": {"self": f"/api/jobs/{self.id}"},
        }


def _iso(t: float | None) -> str | None:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t)) if t else None


def safe_name(filename: str) -> str:
    """A file name that is safe on disk and in URLs, and visible in the conversion list:
    "../My Book (2nd ed).pdf" → "My Book _2nd ed_.pdf". Letters of any script are kept."""
    base = filename.replace("\\", "/").rsplit("/", 1)[-1]
    stem = base[:-4] if base.lower().endswith(".pdf") else base
    # letters, digits and combining marks of any script (Tamil vowel signs are marks)
    stem = "".join(c if unicodedata.category(c)[0] in "LNM" or c in "._- " else "_"
                   for c in stem)
    stem = re.sub(r"_{2,}", "_", stem).strip(" .")
    stem = stem.lstrip("_.").strip() or "document"
    return stem[:120].rstrip(" .") + ".pdf"


def parse_options(raw: dict[str, str], default_engine: str) -> dict[str, Any]:
    """Validated conversion options from query or form fields."""
    engine = (raw.get("engine") or default_engine).strip().lower()
    if engine not in ENGINES:
        raise JobError(400, "bad_engine", f"engine must be one of {', '.join(ENGINES)}")
    if engine == "docling":
        from pdf2xml.extract.base import docling_available

        if not docling_available():
            raise JobError(400, "docling_missing",
                           "Docling is not installed on the server (uv sync --extra ml); "
                           "use engine=heuristic")
    to = [t.strip() for t in (raw.get("to") or ",".join(ALL_TARGETS)).split(",") if t.strip()]
    bad = [t for t in to if t not in ALL_TARGETS]
    if bad or not to:
        raise JobError(400, "bad_targets",
                       f"to must be a comma list of {', '.join(ALL_TARGETS)}")
    try:
        pages = parse_pages(raw.get("pages"))
    except ValueError:
        raise JobError(400, "bad_pages", 'pages must look like "1-3,7"') from None
    overlays = (raw.get("overlays") or "true").strip().lower() not in ("0", "false", "no")
    return {"engine": engine, "to": to, "pages": pages, "overlays": overlays}


# ---------------------------------------------------------------- the conversion process

def _worker_main(inbox: Any, events: Any) -> None:
    """Child process: convert each job it is sent; report stages, then done or failed."""
    import logging

    logging.basicConfig(level=logging.WARNING)
    from pdf2xml.pipeline import ConversionError, convert

    while True:
        msg = inbox.get()
        if msg is None:
            return
        job_id, pdf, out_root, cfg = msg
        events.put((job_id, "running", None))

        def progress(stage: str, j: str = job_id) -> None:
            events.put((j, "stage", stage))

        try:
            convert(Path(pdf), Path(out_root), Config.model_validate(cfg), progress=progress)
        except ConversionError as e:            # rejected: encrypted, scanned …
            events.put((job_id, "failed", str(e)))
        except Exception as e:                  # a bug or a broken PDF: keep the process up
            events.put((job_id, "failed", f"{type(e).__name__}: {e}"))
        else:
            events.put((job_id, "done", None))


class ConversionQueue:
    """Jobs in submission order, run one at a time in a child process."""

    def __init__(self, out_root: Path, *, default_engine: str = "docling",
                 max_upload_mb: int = 300) -> None:
        self.out_root = out_root.resolve()
        self.uploads = self.out_root / "_uploads"
        self.default_engine = default_engine
        self.max_upload = max_upload_mb * 1024 * 1024
        self._ctx = multiprocessing.get_context("spawn")
        self._lock = threading.RLock()
        self._jobs: dict[str, Job] = {}
        self._pending: deque[str] = deque()
        self._current: str | None = None
        self._proc: Any = None
        self._inbox: Any = None
        self._events: Any = None
        self._closed = False
        self._listener = threading.Thread(target=self._listen, name="pdf2xml-jobs",
                                          daemon=True)
        self._listener.start()

    # ------------------------------------------------------------ requests
    def submit(self, data: bytes, filename: str, raw_options: dict[str, str]) -> Job:
        if len(data) > self.max_upload:
            raise JobError(413, "too_large",
                           f"the PDF is larger than {self.max_upload // (1024 * 1024)} MB")
        if b"%PDF-" not in data[:1024]:
            raise JobError(400, "not_pdf", "the upload is not a PDF file")
        options = parse_options(raw_options, self.default_engine)
        job_id = uuid.uuid4().hex[:12]
        name = safe_name(filename or "document.pdf")
        folder = self.uploads / job_id
        folder.mkdir(parents=True, exist_ok=True)
        pdf = folder / name
        pdf.write_bytes(data)
        job = Job(job_id, pdf.stem, filename or name, pdf, options)
        with self._lock:
            self._jobs[job_id] = job
            self._pending.append(job_id)
            self._dispatch()
        return job

    def get(self, job_id: str) -> Job:
        with self._lock:
            if job_id not in self._jobs:
                raise JobError(404, "no_job", f"no job {job_id!r}")
            return self._jobs[job_id]

    def list(self) -> list[Job]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.created, reverse=True)

    def cancel(self, job_id: str) -> Job:
        with self._lock:
            job = self.get(job_id)
            if job.state in ("done", "failed", "cancelled"):
                raise JobError(409, "finished", f"job {job_id} is already {job.state}")
            if job_id in self._pending:
                self._pending.remove(job_id)
            elif job_id == self._current:
                self._stop_worker()             # the only way to stop a running conversion
                self._current = None
            job.state, job.finished, job.error = "cancelled", time.time(), None
            self._dispatch()
            return job

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._proc is not None and self._inbox is not None:
                with contextlib.suppress(OSError, ValueError):
                    self._inbox.put(None)
            self._stop_worker(grace=2.0)

    # ------------------------------------------------------------ the worker
    def _start_worker(self) -> None:
        self._inbox = self._ctx.Queue()
        self._events = self._ctx.Queue()
        self._proc = self._ctx.Process(target=_worker_main, args=(self._inbox, self._events),
                                       name="pdf2xml-convert", daemon=True)
        self._proc.start()

    def _stop_worker(self, grace: float = 0.0) -> None:
        proc = self._proc
        self._proc = self._inbox = self._events = None
        if proc is None:
            return
        if grace:
            proc.join(grace)
        if proc.is_alive():
            proc.kill()
        proc.join(5)

    def _dispatch(self) -> None:
        """Send the next queued job to the worker when it is free (lock held)."""
        if self._closed or self._current is not None or not self._pending:
            return
        job = self._jobs[self._pending.popleft()]
        if self._proc is None or not self._proc.is_alive():
            self._start_worker()
        o = job.options
        cfg = Config(engine=o["engine"], targets=o["to"], pages=o["pages"],
                     overlays=o["overlays"])
        self._current = job.id
        self._inbox.put((job.id, str(job.pdf), str(self.out_root),
                         cfg.model_dump(mode="json")))

    def _listen(self) -> None:
        """Apply the worker's events to the jobs; notice a worker that died."""
        while not self._closed:
            with self._lock:
                events, proc, current = self._events, self._proc, self._current
            if events is None:
                time.sleep(0.2)
                continue
            try:
                job_id, kind, detail = events.get(timeout=0.5)
            except queue.Empty:
                with self._lock:
                    if (current is not None and current == self._current and proc is self._proc
                            and proc is not None and not proc.is_alive()):
                        # forget the dead worker first: finishing starts the next job, which
                        # starts a new worker
                        self._proc = self._inbox = self._events = None
                        self._finish(current, "failed",
                                     "the conversion process stopped unexpectedly "
                                     f"(exit code {proc.exitcode}); the PDF may be too large "
                                     "for the available memory")
                continue
            except (OSError, ValueError, EOFError):
                time.sleep(0.2)               # the queue closed under us: worker was stopped
                continue
            with self._lock:
                job = self._jobs.get(job_id)
                if job is None or job.state == "cancelled":
                    continue
                if kind == "running":
                    job.state, job.started = "running", time.time()
                elif kind == "stage":
                    job.stage = detail
                elif kind in ("done", "failed"):
                    self._finish(job_id, kind, detail)

    def _finish(self, job_id: str, state: str, error: str | None) -> None:
        """Lock held."""
        job = self._jobs[job_id]
        job.state, job.finished, job.error = state, time.time(), error
        if job.started is None:
            job.started = job.finished
        if state == "done":
            job.stage = None
            job.result = self.summary(job.doc)
        if self._current == job_id:
            self._current = None
        self._dispatch()

    # ------------------------------------------------------------ results
    def summary(self, doc: str) -> dict[str, Any]:
        """Overall status and where each output can be viewed and downloaded."""
        d = self.out_root / doc
        report: dict[str, Any] = {}
        if (d / "report.json").exists():
            report = json.loads((d / "report.json").read_text(encoding="utf-8"))
        return {"status": _status(report), "outputs": outputs(doc, d),
                "report": f"/api/report?doc={_q(doc)}",
                "warnings": len(report.get("warnings", [])) + sum(
                    len(e.get("warnings", [])) for e in report.get("exports", {}).values())}


def _q(s: str) -> str:
    from urllib.parse import quote

    return quote(s, safe="")


# output key → (file, stylesheet that renders it, or None when it cannot be previewed)
OUTPUT_FILES: dict[str, tuple[str, str | None]] = {
    "bits": ("40_bits.xml", "proof"),
    "jats": ("41_jats.xml", "proof"),
    "canonical_xml": ("30_canonical.xml", "canonical"),
    "canonical_json": ("20_canonical.json", None),
    "epub": ("50_book.epub", None),
    "docx": ("51_document.docx", None),
}


def outputs(doc: str, folder: Path) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for key, (name, sheet) in OUTPUT_FILES.items():
        if (folder / name).is_file():
            links = {"file": name, "download": f"/files/{_q(doc)}/{name}"}
            if sheet:
                links["view"] = f"/view/{sheet}/{_q(doc)}/{name}"
                links["source"] = f"/api/source?doc={_q(doc)}&file={name}"
                links["validate"] = f"/api/validate?doc={_q(doc)}&file={name}"
            out[key] = links
    return out


def _status(report: dict[str, Any]) -> str:
    """"ok", "check" (valid, with warnings) or "invalid"; "unknown" without a report."""
    if not report:
        return "unknown"
    exports = report.get("exports", {}).values()
    valid = (report.get("coverage", {}).get("ok", False) and report.get("xml_valid", False)
             and all(e.get("valid", False) for e in exports))
    if not valid:
        return "invalid"
    warned = report.get("warnings") or any(e.get("warnings") for e in exports)
    return "check" if warned else "ok"
