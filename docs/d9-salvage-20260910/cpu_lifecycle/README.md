# Repaired CPU and lifecycle calibration

The local CPU domain is now bound to the saved local inventory, not to the remote GPU profile hash. This is an offline derivation change; raw acquisition artifacts are unchanged. Only the 43 cases with fully valid original ledgers are used (22 instances). The six scope-limited native cases are excluded here.

Static CPU inventory is matched by hostname, boot ID and clock domain. It does not provide a calibrated frequency/storage/CPU-contention scaling law.

| Target | Events | Within 25% | Worst error |
|---|---:|---:|---:|
| lifecycle:client_processing | 7128 | 23.64% | 372.37% |
| lifecycle:deployment_start | 43 | 60.47% | 38.38% |
| lifecycle:get_state | 1823 | 87.82% | 78.83% |
| lifecycle:outer_swe_agent | 43 | 72.09% | 53.65% |
| lifecycle:persistent_shell_pid_discovery | 43 | 74.42% | 31.14% |
| lifecycle:runner_process_wrapper | 43 | 72.09% | 53.65% |
| lifecycle:script_read | 305 | 93.44% | 75.45% |
| lifecycle:setup | 43 | 65.12% | 48.86% |
| lifecycle:startup | 43 | 41.86% | 72.97% |
| lifecycle:teardown | 43 | 93.02% | 49.30% |
| runtime_command | 2730 | 78.94% | 99.27% |
| semantic_action | 1780 | 53.54% | 2974.57% |

These are instance-grouped development predictions of command/lifecycle wall boundaries. They are not individual kernel-operation models. Wrapper and nested phase sums must not be interpreted as E2E. The outer SWE-agent target is scored directly and separately.

The low client-processing coverage and large semantic-action worst error show that a coarse median does not describe all mechanisms adequately. These diagnostics do not replace the historical CPU hybrid or claim an improvement over it; the cohorts and boundaries differ. Rare bash-interrupt control has insufficient independent support.

Reproduce with `python3 docs/d9-salvage-20260910/cpu_lifecycle/build.py`. The existing calibration adapter enforces the pinned partition and instance grouping. Input hashes and binding evidence are in `manifest.json` and `binding_report.json`.

**No literal D9 pass or hardware-transfer accuracy is claimed.**
