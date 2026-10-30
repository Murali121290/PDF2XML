"""Container health check: the API answers on /api/health."""

import os
import sys
import urllib.request

port = os.environ.get("PDF2XML_PORT", "8765")
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=4) as r:
        sys.exit(0 if r.status == 200 else 1)
except OSError:
    sys.exit(1)
