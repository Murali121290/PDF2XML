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
from pdf2xml.preview import Viewer, host_allowed, make_server

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


def test_form_with_a_tamil_file_name_and_a_broken_form(api: Api) -> None:
    boundary = "b0undary"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"தமிழ்.pdf\"\r\n\r\n").encode() + PDF.read_bytes() + (
            f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"to\"\r\n\r\n"
            f"json\r\n--{boundary}--\r\n").encode()
    ctype = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    status, _, job = api.call("POST", "/api/convert", body, ctype)
    assert status == 202, job
    assert (job["filename"], job["doc"], job["options"]["to"]) == ("தமிழ்.pdf", "தமிழ்", ["json"])
    assert api.wait(job["id"])["state"] == "done"
    status, _, err = api.call("POST", "/api/convert", body[:200], ctype)     # cut off
    assert (status, err["error"]) == (400, "bad_form")


def test_a_rerun_lists_only_its_own_outputs(api: Api) -> None:
    first = api.wait(api.upload(name="rerun.pdf")["id"])
    assert "docx" in first["result"]["outputs"]
    second = api.wait(api.upload("engine=heuristic&to=json,xml", name="rerun.pdf")["id"])
    assert second["state"] == "done", second["error"]
    assert set(second["result"]["outputs"]) == {"canonical_json", "canonical_xml"}


def test_dns_rebinding_host_is_refused(api: Api) -> None:
    port = api.base.rsplit(":", 1)[1]
    evil = {"Host": f"evil.example:{port}", "Origin": f"http://evil.example:{port}"}
    status, _, data = api.call("GET", "/api/jobs", headers=evil)
    assert (status, data["error"]) == (403, "host")
    status, _, _ = api.call("GET", "/api/docs", headers=evil)
    assert status == 403
    status, _, data = api.call("POST", "/api/convert", PDF.read_bytes(),
                               {**evil, "Content-Type": "application/pdf"})
    assert (status, data["error"]) == (403, "host")
    status, _, _ = api.call("GET", "/api/docs", headers={"Host": f"localhost:{port}"})
    assert status == 200


def test_host_allowed() -> None:
    assert host_allowed("127.0.0.1:8765") and host_allowed("[::1]:8765")
    assert host_allowed("localhost:8765") and host_allowed("LOCALHOST")
    assert host_allowed("192.168.1.20:8765")                  # an address cannot be rebound
    assert host_allowed("proofs.lan:8765", ("proofs.lan",))   # the name given with --host
    assert not host_allowed("evil.example:8765") and not host_allowed("")


def test_queue_keeps_running_after_a_listener_error(tmp_path: Path) -> None:
    jobs = ConversionQueue(tmp_path, default_engine="heuristic")

    def broken(doc: str) -> dict[str, Any]:
        raise RuntimeError("bug while reading the results")

    jobs.summary = broken  # type: ignore[method-assign]
    try:
        ids = [jobs.submit(PDF.read_bytes(), "a.pdf", {"to": "json"}).id for _ in range(2)]
        end = time.time() + 120
        while time.time() < end and any(jobs.get(i).state in ("queued", "running")
                                        for i in ids):
            time.sleep(0.2)
        # the error is logged; both jobs still finish instead of the queue freezing
        assert [jobs.get(i).state for i in ids] == ["done", "done"]
    finally:
        jobs.close()


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


def test_api_only_server_for_a_container(tmp_path: Path) -> None:
    """The deploy setup: only the API, a restricted queue (one engine, four outputs, no QA),
    reached by a service name or domain through a proxy."""
    viewer = Viewer(tmp_path)
    jobs = ConversionQueue(tmp_path, default_engine="heuristic", engines=("heuristic",),
                           targets=("json", "xml", "bits", "jats"), qa=False, views=False)
    server = make_server(viewer, "127.0.0.1", 0, jobs, allowed_hosts=("*",), api_only=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    api = Api(f"http://127.0.0.1:{server.server_address[1]}")
    service = {"Host": "pdf2xml-api:8765"}                  # a docker-compose service name
    try:
        status, _, health = api.call("GET", "/api/health", headers=service)
        assert status == 200 and health["status"] == "ok" and health["converts"]
        job = api.upload("", name="deploy.pdf")
        assert job["options"]["to"] == ["json", "xml", "bits", "jats"]
        assert job["options"]["overlays"] is False
        done = api.wait(job["id"])
        assert done["state"] == "done", done["error"]
        out = done["result"]["outputs"]
        assert set(out) == {"bits", "jats", "canonical_xml", "canonical_json"}
        assert all("view" not in links for links in out.values())
        status, _, xml = api.call("GET", out["bits"]["download"], headers=service)
        assert status == 200 and b"<book" in xml
        _, _, check = api.call("GET", out["jats"]["validate"])
        assert check["errors"] == []
        assert not (tmp_path / "deploy" / "qa").exists()          # no overlays, no QA report
        assert done["result"]["completeness"] is None
        for path in ("/", "/upload", "/view/proof/deploy/40_bits.xml", "/api/pages?doc=deploy"):
            assert api.call("GET", path)[0] == 404, path
        for query, code in (("engine=docling", "bad_engine"), ("to=epub", "bad_targets")):
            status, _, err = api.call("POST", "/api/convert?" + query, PDF.read_bytes(),
                                      {"Content-Type": "application/pdf"})
            assert (status, err["error"]) == (400, code)
    finally:
        server.shutdown()
        jobs.close()
        viewer.close()


def test_any_host_name_behind_a_proxy() -> None:
    assert host_allowed("pdf2xml.example.com", ("*",))
    assert host_allowed("api:8765", ("api",)) and not host_allowed("other:8765", ("api",))
    assert not host_allowed("", ("*",))


def test_safe_names() -> None:
    assert safe_name("../My Book (2nd ed).pdf") == "My Book _2nd ed_.pdf"
    assert safe_name("C:\\docs\\_draft.PDF") == "draft.pdf"          # "_" folders are hidden
    assert safe_name("தமிழ் நூல்.pdf") == "தமிழ் நூல்.pdf"           # any script is kept
    assert safe_name("") == "document.pdf"
