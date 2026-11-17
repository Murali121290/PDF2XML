# Conversion Quality Report

Every conversion writes a versioned audit record (`pdf2xml-conversion-quality/1.0`) in
`quality.json` and a self-contained printable `quality_report.html`. Open the Quality Report
section on Upload & convert or the Full Quality Report dialog in Layout QA. The full report
is fetched on demand so ordinary page navigation and job polling stay small.

The audit includes:

- Source filename, SHA-256, byte size, physical page count, requested/processed/missing pages
  and pages explicitly excluded by selection. A page without extractable text is not assumed blank.
- UTC ISO 8601 timestamps, software versions, engine, stage timings and source/output counts.
  Queued jobs also show queue wait, processing and elapsed time from job acceptance. Upload
  parsing and transfer are outside that elapsed-time scope.
- Paragraph, heading, table, figure, formula, image-object, asset, word, character, font and
  artifact counts. Source token counts and output whitespace word counts need not match.
- Per-page character reconciliation, with removed line hyphens accounted for separately.
  Characters are normalized non-whitespace Unicode counts; this is not a reading-order test.
- Complete preserved artifacts with their text, kinds, pages and bounding boxes. Running heads,
  page numbers and other artifacts can be deliberately omitted from publishing outputs; those
  omissions remain visible in the report and export ledgers.
- Source font usage, sizes in points, embedding, Unicode mappings, nonstandard glyphs, subset
  variants and resource pages. Font issues and every suspicious-character occurrence are retained.
- Independent text-reader differences, missing/unmatched images, missing asset files, uncovered
  ink regions, exporter omissions/rearrangements and all structural QA findings.
- Explicit `not_run` / `not_requested` / `not_generated` results rather than absent sections.
  Visual font fidelity, editorial correctness and comparison to approved reference reading order
  are explicitly unverified unless a separate review establishes them.

## Separate XML validation reports

Each conversion creates `validation_canonical.json`, `validation_bits.json`,
`validation_jats.json` and `validation_content.json`, including for formats not requested.
Each record identifies the schema, requested/generated status, all diagnostics returned by
the validator, line numbers where available, exporter warnings, intentional omissions,
completeness evidence, output byte size and SHA-256. Fatal parser errors may prevent a validator
from discovering subsequent errors; no truncation is added by the report layer.

BITS and JATS use their identified bundled NLM DTDs (BITS 2.2 / JATS 1.4 variants). Canonical
and Content XML use application Relax NG schemas. Schema validity is separate from text/image
completeness. This audit format is not an ISO certification or an editorial approval certificate.
Re-export refreshes output validation evidence and timestamps without implying a fresh PDF audit.

## API and failed conversions

Use authenticated `GET /jobs/{id}/report` for the full audit and
`GET /jobs/{id}/artifacts` for report downloads. A schema-failed job still exposes its diagnostic
reports, while unsuccessful XML outputs are not offered as successful artifacts. If processing
fails before auditing, a failure report shows the error/last stage, unknown counts and checks not run.
Other tenants cannot access these reports.

Diagnostic arrays are complete: no first-20 errors, first-20 font pages, first-eight glyph samples
or first-50 omissions limits are imposed. Checks retain documented detection thresholds and cannot
detect every possible visual or semantic defect. Full reports can be large and contain source text;
apply the same access and retention policy as to conversion outputs.
