"""Verify a running deployment: auth, readiness, real conversion, and artifact downloads."""
import argparse
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--engine", choices=("docling", "heuristic"), default="docling")
    args = parser.parse_args()
    token = os.environ["PDF2XML_SMOKE_TOKEN"]

    def request(path, *, body=None, headers=None):
        req = urllib.request.Request(args.url.rstrip("/") + path, data=body,
            headers={"Authorization": f"Bearer {token}", **(headers or {})})
        with urllib.request.urlopen(req, timeout=15) as response:
            return response.status, response.read()

    assert request("/health/ready")[0] == 200
    try:
        urllib.request.urlopen(args.url.rstrip("/") + "/metrics", timeout=15)
        raise AssertionError("Unauthenticated metrics request was accepted")
    except urllib.error.HTTPError as error:
        assert error.code == 401
    boundary = "pdf2xml-smoke-boundary"
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="smoke.pdf"\r\n'
            'Content-Type: application/pdf\r\n\r\n').encode() + args.pdf.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    status, response = request(f"/jobs?targets=json,xml&engine={args.engine}", body=body,
                                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    assert status == 202
    job_id = json.loads(response)["id"]
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        job = json.loads(request(f"/jobs/{job_id}")[1])
        if job["state"] in ("done", "failed", "cancelled"):
            break
        time.sleep(0.5)
    assert job["state"] == "done", job
    artifacts = json.loads(request(f"/jobs/{job_id}/artifacts")[1])
    assert request(artifacts["canonical_xml"])[0] == 200
    assert json.loads(request(artifacts["canonical_json"])[1])["meta"]
    print(json.dumps({"deployment": "passed", "job_id": job_id, "quality": job.get("quality", {}).get("status")}))


if __name__ == "__main__":
    main()
