"""Exercise the existing web-app contract with real conversions in the API worker."""

from __future__ import annotations

import os
import socket
from collections.abc import Iterator
from concurrent.futures import ProcessPoolExecutor
from functools import partial
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from lxml import etree
from reportlab.pdfgen import canvas

import api
from tests.conftest import PDFS

PDF = PDFS / "simple" / "simple.pdf"


def _deny_worker_network() -> None:
    def blocked(*args, **kwargs):
        with Path(os.environ["PDF2XML_TEST_NETWORK_LOG"]).open("a") as log:
            log.write(f"Unexpected socket connection: {args!r}\n")
        raise AssertionError("Outbound network access is forbidden in conversion tests")

    socket.socket.connect = blocked
    socket.socket.connect_ex = blocked


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    with pytest.MonkeyPatch.context() as patch:
        test_root = tmp_path_factory.mktemp("legacy-api")
        patch.setattr(api, "OUTPUT_ROOT", test_root / "out")
        api.OUTPUT_ROOT.mkdir()
        patch.setenv("HF_HOME", str(test_root / "empty-hf-cache"))
        patch.setenv("PDF2XML_TEST_NETWORK_LOG", str(test_root / "network-attempts.log"))
        patch.setattr(api, "ProcessPoolExecutor",
                      partial(ProcessPoolExecutor, initializer=_deny_worker_network))
        with TestClient(api.app) as session:
            yield session


def upload(client: TestClient, params: dict[str, str] | None = None):
    return client.post("/convert", params={"engine": "heuristic", **(params or {})},
                       files={"file": ("chapter.pdf", PDF.read_bytes(), "application/pdf")})


def test_json_contract_and_epub_docx_with_explicit_heuristic(client: TestClient) -> None:
    # Defaults still request EPUB and DOCX; a missing exporter would fail this request.
    response = upload(client)
    assert response.status_code == 200, response.text
    result = response.json()
    assert set(result) == {"status", "filename", "output_directory", "blocks", "coverage",
                           "xml_valid"}
    assert result["status"] == "success" and result["filename"] == "chapter.pdf"
    assert result["coverage"] == 1 and result["xml_valid"] and result["blocks"] > 0
    assert not Path(result["output_directory"]).exists()  # legacy cleanup after the response
    assert not list(api.OUTPUT_ROOT.iterdir())


@pytest.mark.parametrize(("selection", "target", "root_tag"), [
    ("jats", "jats", "article"),
    ("bits", "bits", "book-part-wrapper"),
    ("rawxml", "xml", "document"),
])
def test_direct_xml_response(client: TestClient, selection: str, target: str, root_tag: str
                             ) -> None:
    response = upload(client, {"targets": f"json,xml,{target}", "return_xml": selection})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/xml")
    assert "attachment" in response.headers["content-disposition"]
    root = etree.fromstring(response.content, etree.XMLParser(no_network=True))
    assert etree.QName(root).localname == root_tag
    assert not list(api.OUTPUT_ROOT.iterdir())


@pytest.mark.parametrize("params", [
    {"engine": "missing"}, {"targets": "missing"}, {"targets": ","},
    {"return_xml": "missing"}, {"pages": "0"}, {"pages": "3-1"}, {"pages": "invalid"},
])
def test_bad_options_use_legacy_detail_error(client: TestClient, params: dict[str, str]) -> None:
    response = upload(client, params)
    assert response.status_code == 400, response.text
    assert "detail" in response.json()
    assert not api.OUTPUT_ROOT.exists() or not list(api.OUTPUT_ROOT.iterdir())


def test_missing_xml_target_still_returns_404(client: TestClient) -> None:
    response = upload(client, {"targets": "json", "return_xml": "jats"})
    assert response.status_code == 404 and "detail" in response.json()


def test_epub_target_still_allows_returning_its_canonical_xml(client: TestClient) -> None:
    response = upload(client, {"targets": "epub", "return_xml": "rawxml"})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/xml")


def test_upload_limit_cleans_partial_input(client: TestClient, monkeypatch: pytest.MonkeyPatch
                                         ) -> None:
    monkeypatch.setattr(api, "MAX_UPLOAD_BYTES", 10)
    response = upload(client)
    assert response.status_code == 413
    assert not list(api.OUTPUT_ROOT.iterdir())


def test_rejected_conversion_cleans_input_and_worker_accepts_next_job(client: TestClient) -> None:
    blank = BytesIO()
    page = canvas.Canvas(blank)
    page.showPage()
    page.save()
    response = client.post("/convert", files={"file": ("scan.pdf", blank.getvalue())})
    assert response.status_code == 500 and "no usable text layer" in response.json()["detail"]
    assert not list(api.OUTPUT_ROOT.iterdir())
    assert upload(client, {"targets": "json,xml"}).status_code == 200


def test_uppercase_extension_and_page_selection(client: TestClient) -> None:
    response = client.post(
        "/convert", params={"engine": "heuristic", "targets": "xml",
                             "return_xml": "rawxml", "pages": "1"},
        files={"file": ("CHAPTER.PDF", PDF.read_bytes(), "application/pdf")},
    )
    assert response.status_code == 200, response.text


def test_non_pdf_extension(client: TestClient) -> None:
    response = client.post("/convert", files={"file": ("chapter.txt", b"text")})
    assert response.status_code == 400


def test_existing_root_and_openapi(client: TestClient) -> None:
    assert "POST /convert" in client.get("/").json()["message"]
    schema = client.get("/openapi.json").json()
    assert schema["info"]["version"] == "0.5.3"
    params = {p["name"]: p for p in schema["paths"]["/convert"]["post"]["parameters"]}
    assert params["engine"]["schema"]["default"] == "docling"
    assert params["targets"]["schema"]["default"] == "json,xml,epub,docx"


@pytest.mark.slow
def test_default_docling_uses_local_models_with_network_blocked(client: TestClient) -> None:
    from pdf2xml.extract.structure_docling import resolve_models_dir

    try:
        resolve_models_dir()
    except ValueError as e:
        pytest.skip(str(e))
    # No engine parameter: this exercises the real API default in its spawned worker.
    response = client.post(
        "/convert", params={"targets": "json,xml,bits,jats", "return_xml": "rawxml"},
        files={"file": ("chapter.pdf", PDF.read_bytes(), "application/pdf")},
    )
    assert response.status_code == 200, response.text
    root = etree.fromstring(response.content)
    assert root.findtext("meta/engine") == "docling"
    # A second request uses the same worker and its already-loaded converter.
    assert client.post(
        "/convert", params={"targets": "json,xml"},
        files={"file": ("chapter.pdf", PDF.read_bytes(), "application/pdf")},
    ).status_code == 200
    assert not Path(os.environ["PDF2XML_TEST_NETWORK_LOG"]).exists()
    assert not list(api.OUTPUT_ROOT.iterdir())
