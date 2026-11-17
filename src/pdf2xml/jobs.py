"""Conversion jobs: PDFs uploaded through the web API, converted one at a time.

The HTTP server answers many requests at once; conversions run in one child process, one after
another. That process keeps Docling's models loaded between jobs (loading them takes ~40 s),
keeps Saxon on a single thread (saxonche needs that), and can be stopped to cancel a running
job without taking the server down. It reports each pipeline stage as it starts, so the web
page can show where a long conversion is.

Uploaded PDFs are kept in ``<out>/_uploads/<job id>/``; results go to ``<out>/<job id>-<name>/``
exactly as ``pdf2xml convert`` writes them, so every other API route works on them unchanged.
Job records persist under _jobs. Interrupted jobs are marked failed after restart.
Only one server process may own a given output root.
"""

from __future__ import annotations

import contextlib
import json
import logging
import multiprocessing
import os
import queue
import re
import shutil
import threading
import time
import unicodedata
import uuid
from collections import deque
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pdf2xml.config import ALL_TARGETS, Config, parse_pages

log = logging.getLogger(__name__)

ENGINES = ("docling", "heuristic", "auto")
STATES = ("queued", "running", "done", "failed", "cancelled")
# pipeline stages in the order they run (``pipeline.convert`` reports them)
STAGES = ("style_layer", "structure_layer", "merge_build", "figures", "xml", "content",
          "epub", "docx", "bits_jats", "completeness", "overlays")


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
    owner: str = "local"

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
            "queue_wait_s": round((self.started or end) - self.created, 3),
            "result": self.result,
            "links": {"self": f"/api/jobs/{self.id}"},
        }


def _iso(t: float | None) -> str | None:
    return datetime.fromtimestamp(t, timezone.utc).isoformat() if t is not None else None


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


def parse_options(raw: dict[str, str], default_engine: str,
                  engines: Sequence[str] = ENGINES, targets: Sequence[str] = ALL_TARGETS,
                  qa: bool = True) -> dict[str, Any]:
    """Validated conversion options from query or form fields. ``engines`` and ``targets``:
    what this server offers (all by default); ``qa``: whether it can draw QA overlays."""
    engine = (raw.get("engine") or default_engine).strip().lower()
    if engine not in engines:
        raise JobError(400, "bad_engine", f"engine must be one of {', '.join(engines)}")
    if engine == "docling":
        from pdf2xml.extract.base import docling_available

        if not docling_available():
            raise JobError(400, "docling_missing",
                           "Docling is not installed on the server (uv sync --extra ml); "
                           "use engine=heuristic")
    to = [t.strip() for t in (raw.get("to") or ",".join(targets)).split(",") if t.strip()]
    table_mode = raw.get("table_mode", "accurate")
    if table_mode not in ("accurate", "fast"):
        raise JobError(400, "bad_table_mode", "table_mode must be accurate or fast")
    bad = [t for t in to if t not in targets]
    if bad or not to:
        raise JobError(400, "bad_targets",
                       f"to must be a comma list of {', '.join(targets)}")
    try:
        pages = parse_pages(raw.get("pages"))
        if pages is not None and (not pages or min(pages) < 1):
            raise ValueError("invalid page selection")
    except ValueError:
        raise JobError(400, "bad_pages", 'pages must look like "1-3,7"') from None
    overlays = qa and (raw.get("overlays") or "false").strip().lower() not in ("0", "false",
                                                                               "no")
    return {"engine": engine, "to": to, "pages": pages, "overlays": overlays, "table_mode": table_mode}


# ---------------------------------------------------------------- the conversion process

def _worker_main(inbox: Any, events: Any) -> None:
    """Child process: convert each job it is sent; report stages, then done or failed."""
    import logging

    logging.basicConfig(level=logging.WARNING)
    from pdf2xml.observability import configure_logging
    configure_logging()
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
            clear_outputs(Path(out_root) / Path(pdf).stem)
            result = convert(Path(pdf), Path(out_root), Config.model_validate(cfg), progress=progress)
            if any(i["code"] in ("canonical_schema", "content_schema", "export_schema")
                   for i in result.quality.get("issues", [])):
                raise ConversionError("Requested output failed mandatory schema validation")
        except ConversionError as e:            # rejected: encrypted, scanned …
            events.put((job_id, "failed", str(e)))
        except Exception as e:                  # a bug or a broken PDF: keep the process up
            events.put((job_id, "failed", f"{type(e).__name__}: {e}"))
        else:
            events.put((job_id, "done", None))


class ConversionQueue:
    """Jobs in submission order, run one at a time in a child process."""

    def __init__(self, out_root: Path, *, default_engine: str = "docling",
                 max_upload_mb: int = 300, models_dir: Path | None = None,
                 grobid_url: str | None = None, engines: Sequence[str] = ENGINES,
                 targets: Sequence[str] = ALL_TARGETS, qa: bool = True,
                 views: bool = True, max_pending: int = 32,
                 max_runtime_s: float = 1800, retention_s: float = 86400,
                 max_pages: int = 1000) -> None:
        """``models_dir``: Docling's pre-downloaded models (offline use). ``grobid_url``: a
        GROBID server for references and article headers. ``engines``/``targets``: what a
        job may ask for; ``qa``: QA overlays and the completeness report; ``views``: list
        XSLT preview links in the results (off on an API-only server)."""
        self.out_root = out_root.resolve()
        self.uploads = self.out_root / "_uploads"
        self.default_engine = default_engine
        self.models_dir = models_dir
        self.grobid_url = grobid_url
        self.engines = tuple(engines)
        self.targets = tuple(targets)
        self.qa = qa
        self.views = views
        self.max_upload = max_upload_mb * 1024 * 1024
        self.max_pending = max_pending
        if max_pending < 1:
            raise ValueError("max_pending must be positive")
        if max_runtime_s <= 0:
            raise ValueError("max_runtime_s must be positive")
        self.max_runtime_s = max_runtime_s
        if retention_s <= 0 or max_pages < 1:
            raise ValueError("retention_s and max_pages must be positive")
        self.retention_s, self.max_pages = retention_s, max_pages
        self._last_cleanup = 0.0
        self._ctx = multiprocessing.get_context("spawn")
        self._lock = threading.RLock()
        self._jobs: dict[str, Job] = {}
        self._pending: deque[str] = deque()
        self._current: str | None = None
        self._proc: Any = None
        self._inbox: Any = None
        self._events: Any = None
        self._closed = False
        self.out_root.mkdir(parents=True, exist_ok=True)
        self._ownership = (self.out_root / ".queue.lock").open("a+b")
        try:
            if self._ownership.seek(0, 2) == 0:
                self._ownership.write(b"0")
                self._ownership.flush()
            self._ownership.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._ownership.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._ownership.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._ownership.close()
            raise RuntimeError("Another job service owns this output root; use one server process.") from None
        self._restore()
        self._listener = threading.Thread(target=self._listen, name="pdf2xml-jobs",
                                          daemon=True)
        self._listener.start()

    def _persist(self, job: Job) -> None:
        folder = self.out_root / "_jobs"
        folder.mkdir(parents=True, exist_ok=True)
        data = asdict(job)
        data["pdf"] = str(job.pdf)
        destination = folder / f"{job.id}.json"
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        temporary.replace(destination)

    def _restore(self) -> None:
        # One queue owns this output root. Interrupted work is never reported as successful.
        for path in (self.out_root / "_jobs").glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                data["pdf"] = Path(data["pdf"])
                job = Job(**data)
                if job.state in ("queued", "running"):
                    job.state, job.finished = "failed", time.time()
                    job.error = "Server restarted before conversion completed; resubmit the PDF."
                    self._persist(job)
                self._jobs[job.id] = job
            except (OSError, ValueError, TypeError):
                log.exception("could not restore job record %s", path.name)

    # ------------------------------------------------------------ requests
    def submit(self, data: bytes | memoryview, filename: str,
               raw_options: dict[str, str], *, owner: str = "local") -> Job:
        if len(data) > self.max_upload:
            raise JobError(413, "too_large",
                           f"the PDF is larger than {self.max_upload // (1024 * 1024)} MB")
        if b"%PDF-" not in bytes(data[:1024]):
            raise JobError(400, "not_pdf", "the upload is not a PDF file")
        options = parse_options(raw_options, self.default_engine, self.engines, self.targets,
                                self.qa)
        job_id = uuid.uuid4().hex[:12]
        name = f"{job_id}-{safe_name(filename or 'document.pdf')}"
        folder = self.uploads / job_id
        with self._lock:
            if self._closed or len(self._pending) + bool(self._current) >= self.max_pending:
                raise JobError(503, "queue_full", "Conversion queue is full; retry later.")
            folder.mkdir(parents=True, exist_ok=True)
            pdf = folder / name
            pdf.write_bytes(data)
            job = Job(job_id, pdf.stem, filename or name, pdf, options, owner=owner)
            self._persist(job)
            self._jobs[job_id] = job
            self._pending.append(job_id)
            log.info("job_submitted", extra={"job_id": job_id, "state": "queued"})
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
            self._persist(job)
            self._dispatch()
            return job

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._proc is not None and self._inbox is not None:
                with contextlib.suppress(OSError, ValueError):
                    self._inbox.put(None)
            self._stop_worker(grace=2.0)
        self._listener.join(timeout=2)
        self._ownership.close()

    # ------------------------------------------------------------ the worker
    def _start_worker(self) -> None:
        self._inbox = self._ctx.Queue()
        self._events = self._ctx.Queue()
        self._proc = self._ctx.Process(target=_worker_main, args=(self._inbox, self._events),
                                       name="pdf2xml-convert", daemon=True)
        self._proc.start()

    def _stop_worker(self, grace: float = 0.0) -> None:
        proc = self._proc
        channels = (self._inbox, self._events)
        self._proc = self._inbox = self._events = None
        if proc is None:
            return
        if grace:
            proc.join(grace)
        if proc.is_alive():
            proc.kill()
        proc.join(5)
        for channel in channels:
            if channel is not None:
                with contextlib.suppress(OSError, ValueError, AttributeError):
                    channel.cancel_join_thread()
                    channel.close()

    def _dispatch(self) -> None:
        """Send the next queued job to the worker when it is free (lock held). A job that
        cannot be sent fails at once, so it never waits in "queued" for nothing."""
        while not self._closed and self._current is None and self._pending:
            job = self._jobs[self._pending.popleft()]
            try:
                if self._proc is None or not self._proc.is_alive():
                    self._start_worker()
                o = job.options
                cfg = Config(engine=o["engine"], targets=o["to"], pages=o["pages"],
                             table_mode=o.get("table_mode", "accurate"),
                             overlays=o["overlays"], completeness=self.qa,
                             models_dir=self.models_dir, grobid_url=self.grobid_url,
                             cache_dir=self.out_root / "_cache", cache_scope=job.owner,
                             max_pages=self.max_pages, debug_artifacts=False)
                self._inbox.put((job.id, str(job.pdf), str(self.out_root),
                                 cfg.model_dump(mode="json")))
            except Exception as e:
                log.exception("could not start job %s", job.id)
                self._stop_worker()
                job.state, job.finished = "failed", time.time()
                job.started = job.started or job.finished
                job.error = f"the conversion process could not be started: {e}"
                self._persist(job)
                continue
            self._current = job.id

    def _listen(self) -> None:
        """Apply the worker's events to the jobs; notice a worker that died. An error here
        is logged and the loop goes on: a dead listener would leave every job queued."""
        while not self._closed:
            try:
                if time.time() - self._last_cleanup > 60:
                    self.cleanup()
                self._listen_once()
            except Exception:
                log.exception("job listener error")
                time.sleep(0.2)

    def _listen_once(self) -> None:
        with self._lock:
            events, proc, current = self._events, self._proc, self._current
        if events is None:
            time.sleep(0.2)
            return
        try:
            job_id, kind, detail = events.get(timeout=0.5)
        except queue.Empty:
            with self._lock:
                job = self._jobs.get(current) if current else None
                if (job and job.started and current == self._current
                        and time.time() - job.started > self.max_runtime_s):
                    self._stop_worker()
                    self._current = None
                    self._finish(current, "failed", "Conversion exceeded its processing deadline.")
                    return
                if (current is not None and current == self._current and proc is self._proc
                        and proc is not None and not proc.is_alive()):
                    # forget the dead worker first: finishing starts the next job, which
                    # starts a new worker
                    self._proc = self._inbox = self._events = None
                    self._finish(current, "failed",
                                 "the conversion process stopped unexpectedly "
                                 f"(exit code {proc.exitcode}); the PDF may be too large "
                                 "for the available memory")
            return
        except (OSError, ValueError, EOFError):
            time.sleep(0.2)               # the queue closed under us: worker was stopped
            return
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.state == "cancelled":
                return
            if kind == "running":
                job.state, job.started = "running", time.time()
            elif kind == "stage":
                job.stage = detail
            elif kind in ("done", "failed"):
                self._finish(job_id, kind, detail)
            self._persist(job)

    def _finish(self, job_id: str, state: str, error: str | None) -> None:
        """Lock held."""
        job = self._jobs[job_id]
        job.state, job.finished, job.error = state, time.time(), error
        if job.started is None:
            job.started = job.finished
        if self._current == job_id:
            self._current = None
        try:
            folder = self.out_root / job.doc
            quality_path = folder / "quality.json"
            if state == "failed" and not quality_path.exists():
                from pdf2xml.qa.report import failure_report
                folder.mkdir(parents=True, exist_ok=True)
                quality_path.write_text(json.dumps(failure_report(job.filename, job.error or "Conversion failed",
                                       job.stage, job.options.get("to", [])), ensure_ascii=False, indent=2), encoding="utf-8")
            if quality_path.is_file():
                from pdf2xml.qa.report import html_report
                quality = json.loads(quality_path.read_text(encoding="utf-8"))
                quality.setdefault("source", {})["original_filename"] = job.filename
                quality.setdefault("runtime", {})["job"] = {
                    "id": job.id, "created_at_epoch_s": job.created,
                    "created_at": _iso(job.created), "started_at": _iso(job.started), "finished_at": _iso(job.finished),
                    "started_at_epoch_s": job.started, "finished_at_epoch_s": job.finished,
                    "queue_wait_s": max(0, job.started - job.created),
                    "processing_s": max(0, job.finished - job.started),
                    "total_elapsed_s": max(0, job.finished - job.created),
                    "scope": "From job acceptance; excludes HTTP upload parsing and transfer."}
                if state == "failed":
                    quality["status"] = "failed"
                quality_path.write_text(json.dumps(quality, ensure_ascii=False, indent=2), encoding="utf-8")
                (folder / "quality_report.html").write_text(html_report(quality), encoding="utf-8")
                report_path = folder / "report.json"
                if report_path.is_file():
                    report = json.loads(report_path.read_text(encoding="utf-8"))
                    report["quality"] = quality
                    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            if state == "done":
                job.stage = None
                job.result = self.summary(job.doc)
        except (OSError, ValueError) as e:      # an unreadable report.json
            job.state = "failed"
            job.error = f"converted, but the results could not be read: {e}"
        finally:
            self._persist(job)
            log.info("job_finished", extra={"job_id": job.id, "state": job.state,
                     "elapsed_s": job.to_json()["elapsed_s"], "queue_wait_s": job.to_json()["queue_wait_s"]})
            self._dispatch()

    def cleanup(self) -> int:
        """Expire terminal jobs only; never traverse outside this queue's owned directories."""
        removed = 0
        with self._lock:
            self._last_cleanup = time.time()
            cutoff = self._last_cleanup - self.retention_s
            for job in list(self._jobs.values()):
                if job.state not in ("done", "failed", "cancelled") or not job.finished or job.finished > cutoff:
                    continue
                for target, parent in ((self.out_root / job.doc, self.out_root),
                                       (self.uploads / job.id, self.uploads)):
                    resolved = target.resolve()
                    if target.is_symlink() or resolved.parent != parent.resolve():
                        raise ValueError("Unsafe retention target")
                    if target.is_dir():
                        shutil.rmtree(target)
                (self.out_root / "_jobs" / f"{job.id}.json").unlink(missing_ok=True)
                del self._jobs[job.id]
                removed += 1
            cache = self.out_root / "_cache"
            if cache.is_dir() and not cache.is_symlink() and self._current is None:
                for item in cache.glob("*.json"):
                    if not item.is_symlink() and item.stat().st_mtime < cutoff:
                        item.unlink()
        return removed

    def metrics(self, owner: str) -> dict[str, Any]:
        with self._lock:
            jobs = [j for j in self._jobs.values() if j.owner == owner]
            now = time.time()
            recent = [j for j in jobs if j.finished and j.finished > now - 900 and j.state in ("done", "failed")]
            waiting = [now - j.created for j in jobs if j.state == "queued"]
            return {"states": {state: sum(j.state == state for j in jobs) for state in STATES},
                    "needs_review": sum(bool(j.result and (j.result.get("quality") or {}).get("status") == "needs_review") for j in jobs),
                    "capacity": self.max_pending, "oldest_queue_wait_s": max(waiting, default=0),
                    "recent_completed": len(recent),
                    "recent_failures": sum(j.state == "failed" for j in recent),
                    "listener_alive": self._listener.is_alive()}

    # ------------------------------------------------------------ results
    def summary(self, doc: str) -> dict[str, Any]:
        """Overall status and where each output can be viewed and downloaded."""
        d = self.out_root / doc
        report = json.loads((d / "report.json").read_text(encoding="utf-8"))
        return {"status": _status(report), "outputs": outputs(doc, d, self.views),
                "report": f"/api/report?doc={_q(doc)}",
                "quality": {key: report.get("quality", {}).get(key) for key in ("status", "errors", "warnings", "issue_count")},
                # ok | info | warn | error, details in qa/completeness.json; None when not run
                "completeness": report.get("completeness", {}).get("status"),
                "warnings": len(report.get("warnings", [])) + sum(
                    len(e.get("warnings", [])) for e in report.get("exports", {}).values())}


def _q(s: str) -> str:
    from urllib.parse import quote

    return quote(s, safe="")


# output key → (file, stylesheet that renders it, or None when it cannot be previewed)
OUTPUT_FILES: dict[str, tuple[str, str | None]] = {
    "quality": ("quality.json", None),
    "quality_html": ("quality_report.html", None),
    "report": ("report.json", None),
    "validation_canonical": ("validation_canonical.json", None),
    "validation_bits": ("validation_bits.json", None),
    "validation_jats": ("validation_jats.json", None),
    "validation_content": ("validation_content.json", None),
    "completeness": ("qa/completeness.json", None),
    "bits": ("40_bits.xml", "proof"),
    "jats": ("41_jats.xml", "proof"),
    "canonical_xml": ("30_canonical.xml", "canonical"),
    "content_xml": ("42_content.xml", "content"),
    "canonical_json": ("20_canonical.json", None),
    "epub": ("50_book.epub", None),
    "docx": ("51_document.docx", None),
}


def clear_outputs(folder: Path) -> None:
    """Remove the outputs of an earlier conversion with the same name, so a job never lists
    a file it did not write (e.g. a DOCX from a run that asked for it). The Docling cache
    stays: it is checked against the PDF's hash before it is reused."""
    for name in [*(f for f, _ in OUTPUT_FILES.values()), "report.json", "source.json"]:
        (folder / name).unlink(missing_ok=True)
    for sub in ("assets", "qa"):
        sub_dir = folder / sub
        if sub_dir.is_dir():
            shutil.rmtree(sub_dir, ignore_errors=True)


def outputs(doc: str, folder: Path, views: bool = True) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for key, (name, sheet) in OUTPUT_FILES.items():
        if (folder / name).is_file():
            links = {"file": name, "download": f"/files/{_q(doc)}/{name}"}
            if sheet:
                if views:
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
    warned = (report.get("quality", {}).get("status") == "needs_review"
              or report.get("warnings") or any(e.get("warnings") for e in exports))
    return "check" if warned else "ok"
