"""Conversion API: upload a PDF, follow the job, get the outputs; refusals; cancel; CORS.

The server runs for real (HTTP on a free port, conversions in the child process) with the
heuristic engine, so the tests take seconds; Docling follows the same path.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from pdf2xml.jobs import ConversionQueue, safe_name
from pdf2xml.preview import Viewer, make_server

PDF = Path(__file__).resolve().parents[1] / "fixtures" / "pdfs" / "simple" / "simple.pdf"


class Api:
    def __init__(self, base: str) -> None:
        self.base = base

    def call(self, method: str, path: str, body: bytes | None = None,
             headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], Any]:
        req = urllib.request.Request(self.base + path, data=body, method=method,
                                     headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw, status, hdrs = r.read(), r.status, dict(r.headers)
        except urllib.error.HTTPError as e:
            raw, status, hdrs = e.read(), e.code, dict(e.headers)
        ctype = hdrs.get("Content-Type", "")
        return status, hdrs, json.loads(raw) if "json" in ctype and raw else raw

    def upload(self, query: str = "engine=heuristic", name: str = "simple.pdf") -> Any:
        status, hdrs, job = self.call("POST", "/api/convert?" + query, PDF.read_bytes(),
                                      {"Content-Type": "application/pdf", "X-Filename": name})
        assert status == 202, job
        assert hdrs["Location"] == f"/api/jobs/{job['id']}"
        return job

    def wait(self, job_id: str, timeout: float = 180) -> Any:
        end = time.time() + timeout
        while time.time() < end:
            _, _, job = self.call("GET", f"/api/jobs/{job_id}")
            if job["state"] not in ("queued", "running"):
                return job
            time.sleep(0.3)
        raise AssertionError(f"job {job_id} did not finish: {job}")


@pytest.fixture(scope="module")
def api(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Api]:
    out = tmp_path_factory.mktemp("out")
    viewer = Viewer(out)
    jobs = ConversionQueue(out, default_engine="heuristic")
    server = make_server(viewer, "127.0.0.1", 0, jobs, cors=("http://localhost:5173",))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield Api(f"http://127.0.0.1:{server.server_address[1]}")
    server.shutdown()
    jobs.close()
    viewer.close()


def test_upload_convert_and_show_outputs(api: Api) -> None:
    job = api.upload(name="My%20Book%20(v2).pdf")               # X-Filename is URL-encoded
    assert (job["state"], job["doc"], job["filename"]) == ("queued", "My Book _v2_",
                                                           "My Book (v2).pdf")
    done = api.wait(job["id"])
    assert done["state"] == "done", done["error"]
    assert done["result"]["status"] in ("ok", "check")
    out = done["result"]["outputs"]
    assert {"bits", "jats", "canonical_xml", "canonical_json", "epub", "docx"} <= set(out)
    assert out["bits"]["view"] == "/view/proof/My%20Book%20_v2_/40_bits.xml"
    status, hdrs, html = api.call("GET", out["bits"]["view"])
    assert status == 200 and hdrs["Content-Type"].startswith("text/html") and b"<html" in html
    status, _, xml = api.call("GET", out["jats"]["download"])
    assert status == 200 and b"<article" in xml
    _, _, check = api.call("GET", out["bits"]["validate"])
    assert check == {"schema": "bits", "errors": []}
    _, _, docs = api.call("GET", "/api/docs")
    assert "My Book _v2_" in [d["name"] for d in docs["docs"]]      # _uploads stays hidden


def test_multipart_form_upload(api: Api) -> None:
    boundary = "----pdf2xmltest"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"engine\"\r\n\r\n"
            f"heuristic\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"to\"\r\n\r\n"
            f"json,xml,bits,jats\r\n--{boundary}\r\nContent-Disposition: form-data; "
            f"name=\"file\"; filename=\"form.pdf\"\r\nContent-Type: application/pdf\r\n\r\n"
            ).encode() + PDF.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    status, _, job = api.call("POST", "/api/convert", body,
                              {"Content-Type": f"multipart/form-data; boundary={boundary}"})
    assert status == 202, job
    assert job["options"]["to"] == ["json", "xml", "bits", "jats"]
    done = api.wait(job["id"])
    assert done["state"] == "done", done["error"]
    assert "epub" not in done["result"]["outputs"] and "bits" in done["result"]["outputs"]


def test_refusals_are_json(api: Api) -> None:
    cases = [
        ("POST", "/api/convert", b"hello", {"Content-Type": "application/pdf"}, 400, "not_pdf"),
        ("POST", "/api/convert?engine=magic", PDF.read_bytes(),
         {"Content-Type": "application/pdf"}, 400, "bad_engine"),
        ("POST", "/api/convert?to=pdf", PDF.read_bytes(),
         {"Content-Type": "application/pdf"}, 400, "bad_targets"),
        ("POST", "/api/convert", b"", {"Content-Type": "application/pdf"}, 411, "no_body"),
        ("GET", "/api/jobs/nope", None, {}, 404, "no_job"),
        ("DELETE", "/api/jobs/nope", None, {}, 404, "no_job"),
        # a page on another web site must not be able to upload to this local server
        ("POST", "/api/convert", PDF.read_bytes(),
         {"Content-Type": "application/pdf", "Origin": "https://evil.example"}, 403, "origin"),
    ]
    for method, path, body, headers, want_status, want_code in cases:
        status, _, data = api.call(method, path, body, headers)
        assert (status, data["error"]) == (want_status, want_code), (path, data)
        assert data["message"]


def test_cancel_a_queued_job(api: Api) -> None:
    first = api.upload()
    second = api.upload()
    status, _, cancelled = api.call("DELETE", f"/api/jobs/{second['id']}")
    assert (status, cancelled["state"]) == (200, "cancelled")
    status, _, again = api.call("POST", f"/api/jobs/{second['id']}/cancel")
    assert (status, again["error"]) == (409, "finished")
    assert api.wait(first["id"])["state"] == "done"
    _, _, listing = api.call("GET", "/api/jobs")
    states = {j["id"]: j["state"] for j in listing["jobs"]}
    assert states[second["id"]] == "cancelled"


def test_cors_for_a_frontend_dev_server(api: Api) -> None:
    origin = {"Origin": "http://localhost:5173"}
    status, hdrs, _ = api.call("OPTIONS", "/api/convert",
                               headers={**origin, "Access-Control-Request-Method": "POST"})
    assert status == 204
    assert hdrs["Access-Control-Allow-Origin"] == "http://localhost:5173"
    assert "POST" in hdrs["Access-Control-Allow-Methods"]
    status, hdrs, _ = api.call("GET", "/api/jobs", headers=origin)
    assert status == 200 and hdrs["Access-Control-Allow-Origin"] == "http://localhost:5173"
    status, _, _ = api.call("OPTIONS", "/api/convert", headers={"Origin": "https://other.example"})
    assert status == 403


def test_upload_page_is_served(api: Api) -> None:
    status, hdrs, html = api.call("GET", "/upload")
    assert status == 200 and b"/api/convert" in html


def test_safe_names() -> None:
    assert safe_name("../My Book (2nd ed).pdf") == "My Book _2nd ed_.pdf"
    assert safe_name("C:\\docs\\_draft.PDF") == "draft.pdf"          # "_" folders are hidden
    assert safe_name("தமிழ் நூல்.pdf") == "தமிழ் நூல்.pdf"           # any script is kept
    assert safe_name("") == "document.pdf"
