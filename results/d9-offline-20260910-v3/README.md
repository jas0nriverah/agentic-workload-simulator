# Checked D9 model opportunities — v3

Read [the report](model_opportunities/REPORT.md) and [review](model_opportunities/REVIEW.md).

This revision tests CPU mechanisms, Astropy-to-Django transfer, openat entry flags, the first-request GPU hypothesis, and E2E reconstruction. The openat flags/path candidate improves coverage on both tested repositories; full individual-event accuracy and hardware transfer remain unvalidated. All 1,784 eligible raw client/proxy/native joins are recovered; proxy finalization boundaries require care in E2E modeling.

Download and extract `d9-offline-20260910-v3.tar.gz` for the complete runnable source/dependency layout. The folders here are browsable analysis outputs; use the extracted archive to run tools. Large raw CPU files and repeated external journals remain separately retained.

Validation: 20 new tests passed in the workspace, 15 passed from an isolated extraction, all 404 archive file hashes verified, and a saved openat prediction was reconstructed without raw traces. Raw E2E audit regeneration still requires the retained journals. No new inference or frozen acquisition changes. **Literal D9 is not met.**
