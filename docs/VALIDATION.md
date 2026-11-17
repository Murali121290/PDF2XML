# Validation recorded on this Windows development machine

## Automated checks

- 46 regression tests passed, including tenant isolation, bounded uploads, restart records,
  cancellation, deadlines, retention, cache corruption/invalidation, page limits and native
  table/index text preservation.
- `pip check` reported no broken requirements.
- Live localhost HTTP smoke tests passed with both heuristic and real Docling child workers.
  The Docling result correctly retained its review status.
- The Accurate synthetic corpus passed text presence/order, table-cell, coverage and XML checks.
- Fast mode failed the synthetic table-cell/order checks. It remains explicit opt-in and must
  not be substituted for Accurate without acceptance testing.
- Workflow, Compose and monitoring files parse as YAML. Docker and `promtool` are not installed
  here, so image execution and Prometheus rule compilation remain unverified locally.

## Supplied 280-page production book

The initial full Accurate conversion took 646.7 seconds. Docling structure inference accounted
for 607.1 seconds; native style extraction accounted for 31.7 seconds. Sampled peak process RSS
was about 4.03 GiB. This is a development-machine measurement, not an SLA or capacity estimate.

That first run exposed a real merge defect: three empty/unboxed table predictions discarded
native text on index pages 273, 275 and 276. Coverage was 99.451%. The merge fallback now uses
native PDF words. On bookmarked index pages with an empty prediction and confirmed column
geometry, recovery preserves native column/line order rather than inventing table cells.

Verification reruns reused raw extraction and reran recovery, merging, exports and QA. Cached
runs ranged from about 32 to 57 seconds in this session; the latest run was 57.3 seconds, with
native style extraction responsible for 40.4 seconds. Final character coverage is 100%, canonical
and Content XML validate, and QA reports zero errors and 34 review warnings:

- 29 layout-confidence warnings.
- Three index-line recovery warnings: index-entry semantics and links remain unverified.
- Two numeral-heading relationship warnings at bookmarked section starts.

Character coverage does not prove reading order or editorial correctness. No approved reference
XML was provided, so the production benchmark records reference-order and reference-table checks
as unavailable. Body and recovered index page overlays were visually inspected. An editor must
still review the flagged pages. First-time full-book inference remains expensive on CPU.

Local evidence is under `out/benchmark-production/`: `baseline-before-fix.json` preserves the
initial run and `benchmark.json` records the final verification. The final canonical document,
XML, Content XML, `quality.json` and `report.json` are under its `case-0-run-0` document folder.
These contain user document data and must not be published as CI artifacts or committed.

## Deployment completion

Before exposing the service, configure real tenant credentials and TLS ingress, run the Docker
smoke checks from OPERATIONS.md, connect monitoring receivers and review the document warnings.
CI definitions and monitoring configuration are supplied; they have not been executed against an
external deployment or monitoring account in this session. Single-host ownership is intentional;
multi-host scheduling and transparent retries require a separate durable scheduler design.
