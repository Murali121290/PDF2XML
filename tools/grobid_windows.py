"""Let a GROBID source tree build and run on Windows (for `pdf2xml --grobid`).

GROBID's build.gradle stops with "Unsupported platform!" on Windows, although its CRF models
run there: Wapiti ships as grobid-home/lib/win-64/libwapiti.dll and is loaded by absolute path.
This adds the Windows branch to both platform checks. Only the reference parser
(processCitationList) works this way: pdfalto, which reads PDFs for processHeaderDocument, has
no Windows build, so article authors need GROBID on Linux or in Docker.

    python tools/grobid_windows.py C:\\path\\to\\grobid-0.9.1

Then, with a JDK 21 as JAVA_HOME (behind a TLS-inspecting proxy add
JAVA_TOOL_OPTIONS=-Djavax.net.ssl.trustStoreType=Windows-ROOT so Java trusts the Windows
certificate store):

    gradlew.bat clean install -x test
    gradlew.bat :grobid-service:run          # http://localhost:8070
"""

from __future__ import annotations

import sys
from pathlib import Path

THROW = 'throw new RuntimeException("Unsupported platform!")'
MARK = "// FAMILY_WINDOWS (pdf2xml tools/grobid_windows.py)"
FIXES = ('jepLocalLibraries = "${file("./grobid-home/lib/win-64").absolutePath}" ',
         'libraries = "${file("../grobid-home/lib/win-64").absolutePath}" ')


def patch(root: Path) -> str:
    path = root / "build.gradle"
    text = path.read_text(encoding="utf-8")
    if MARK in text or "FAMILY_WINDOWS" in text:
        return "already patched"
    if text.count(THROW) != len(FIXES):
        raise SystemExit(f"{path}: expected {len(FIXES)} platform checks, found "
                         f"{text.count(THROW)}; this GROBID version needs a manual change")
    for fix in FIXES:
        k = text.index(THROW)
        text = text[:k] + fix + MARK + text[k + len(THROW):]
    path.write_text(text, encoding="utf-8")
    return "patched"


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    print(patch(Path(sys.argv[1])))
