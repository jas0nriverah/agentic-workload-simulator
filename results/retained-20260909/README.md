# Retained results snapshot — 2026-09-09

This compact packet contains accepted offline output-v4 reports, D1–D8 historical/current tables and figures, output-v4 D9 calibration-pending evidence, selected analysis generators, and a sanitized index of the currently accepted comparison bindings. Every included file is listed with a source path and SHA-256 in MANIFEST.json. Local absolute paths in copied reports and generators are replaced with <REDACTED_HOME>; source SHA-256 values retain provenance to the unmodified accepted artifacts.

## Status and limits

This is an **interim snapshot, not final results**. The authoritative read-only queue snapshot at 2026-09-09T20:25:20Z records 55 queue-accepted cases plus one separately adjudicated result (56 scientifically validated total), one blocked case, and 40 pending cases; no attempt was running in that SQLite snapshot. The final 1,088-case run had not started.

D9 compliance is unproven. Output-v4 reports analysis_complete_calibration_pending: zero eligible repaired training cases, no fit, no evaluator score, no new H100 inference, and no established hardware transfer or blind-test accuracy. D1–D8 materials are retained descriptive/historical evidence; they are not an assertion that unproven D9 requirements have been met.

## Contents and exclusions

offline-followup-20260909/output-v4 is the accepted offline regeneration. Earlier output-v1/v2 failures and the interrupted output-v3 are deliberately excluded. The artifact manifest may name omitted ledger files; those raw event/ledger streams are intentionally not in this packet.

comparison/ is derived from the current queue and comparison evidence. It deliberately omits endpoints, URLs, artifact paths, raw traces, case-claim secrets, and environment data. historical-renderer/ and the copied analysis-code map are compact reproduction aids only; frozen acquisition inputs and source trees are not bundled.

No file exceeds 10 MB; the packet is intended to remain below 30 MB.
