"""Start an isolated local API with ephemeral credentials and run deployment smoke checks."""
import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--engine", choices=("docling", "heuristic"), default="docling")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    folder = root / "out" / f"smoke-{secrets.token_hex(6)}"
    folder.mkdir(parents=True)
    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        port = port_socket.getsockname()[1]
    env = os.environ.copy()
    token = secrets.token_urlsafe(32)
    env.update(PDF2XML_API_KEYS=json.dumps({"smoke": token}), PDF2XML_SMOKE_TOKEN=token,
               PDF2XML_DATA=str(folder / "data"))
    env.pop("PDF2XML_ALLOW_ANONYMOUS", None)
    url = f"http://127.0.0.1:{port}"
    with (folder / "server.log").open("w", encoding="utf-8") as log:
        server = subprocess.Popen([sys.executable, "tools/run_api.py", "--host", "127.0.0.1",
                                   "--port", str(port)], cwd=root, env=env, stdout=log, stderr=log,
                                  creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if server.poll() is not None:
                    raise RuntimeError(f"Server exited; inspect {folder / 'server.log'}")
                try:
                    with urllib.request.urlopen(url + "/health/ready", timeout=2):
                        break
                except OSError:
                    time.sleep(0.25)
            else:
                raise RuntimeError("Readiness deadline exceeded")
            subprocess.run([sys.executable, "tools/deployment_smoke.py", url, str(args.pdf.resolve()),
                            "--engine", args.engine], cwd=root, env=env, check=True)
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()


if __name__ == "__main__":
    main()
