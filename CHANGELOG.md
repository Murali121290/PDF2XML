# Changes

## Offline Docling default — 2026-10-22

- Make Docling the API, pipeline and CLI default while preserving the existing HTTP contract.
- Resolve only complete local models; disable runtime downloads, telemetry, remote services
  and external plugins. Missing weights fail with a model-preparation instruction.
- Pin model revisions/checksums and provide `tools/prepare_models.py` for one-time downloads,
  copying existing weights, or offline verification. Keep large weights outside Git.
- Bake verified models into the Docker image, enforce offline settings, and run a build
  self-test with networking disabled. Reuse one conversion worker after the first model load.
- Test default Docling requests with an empty model-hub cache and blocked outbound sockets.

## 0.5.3 compatibility update — 2026-10-22

- Port the 0.5.3 Docling model configuration, page-aware layout cache, text merge,
  image recovery, and BITS/JATS publishing improvements into the existing `src/` package.
- Preserve `/convert`, `file`, `engine`, `targets`, `return_xml`, direct XML responses,
  JSON response fields, the heuristic default engine, and port 8087.
- Preserve EPUB/DOCX exports, heuristic extraction, overlays, and the proof viewer.
- Use one persistent conversion process so ML and XSLT work does not block HTTP handling.
- Add optional page selection, upload limits, output-root settings, and CORS configuration.
- Clean up partially written uploads and failed conversions; reject invalid options before
  starting the conversion.
- Keep reference parsing local; remove external reference-service hooks and settings.
- Replace the machine-specific requirements file with a portable editable project install
  and pin the Docling extra to the release's version.
- Add legacy API and conversion regression tests.
