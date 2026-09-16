# Materialized four-fixture inputs

Main adopted materializing the four workload classes on 2026-09-09. The
original plan's identities, three paired repetitions, off/on–on/off–off/on
order, and 5% median / 10% nearest-rank p95 gate remain fixed. No workload is
resized from measured overhead, and no sleep is added to dilute overhead.

`scripts/validation/build_fixed_work_fixtures.py` builds only into a new
directory. It freezes real action/request JSONL, deterministic reset tar
archives, CPU runtime sidecars, source hashes, and a strict v2 runner manifest.
Archives use sorted members and fixed modes, timestamps and owners. There are
no random seeds or outcome-derived inputs. Existing bundles are never changed.

| Fixture | Fixed input and work |
|---|---|
| `cpu-file-traversal-v1` | 24 seed files × 4,096 bytes; `python3 file_traversal.py` reads them, writes/renames 24 outputs, traverses and reads the outputs; `find output -type f \| LC_ALL=C sort \| wc -l`. Two actions. |
| `cpu-test-script-subprocess-v1` | 20 seed files × 8,192 bytes; execute script v1, perform one exact v1→v2 edit, execute v2, run the fixed 15-test `test_linux_work.py` scope, `find \| sort \| wc`, and a checked Python child process. Six actions. |
| `model-short-request-v1` | One payload-hash verification action and one fixed 512-prompt-token chat request; 128 output tokens requested. |
| `model-long-context-request-v1` | One payload-hash verification action and one fixed 60,000-prompt-token chat request; 128 output tokens requested. |

The filesystem recipes adapt archived 24×4,096 and 20×8,192 diagnostics to
relative paths in restored snapshots. The test scope comes from the archived
pytest diagnostic; its exact current test file, `linux_work.py`, and `clock.py`
are copied and hashed. Its 15 unittest-compatible tests use the standard
library runner, avoiding a new pytest dependency inside the pinned image.
Empty package initializers isolate this scope from optional telemetry imports.
Script contents and commands are retained in the builder and snapshot inventory.

These fixtures cover the declared filesystem, test/script/subprocess, short
request and long-context request classes. They do not establish a population
latency distribution. The long prompt repeats a fixed source-code corpus and
may end mid-line; that limitation is recorded. Historical diagnostic timings
are provenance, not baseline timings for these new inputs. Baseline durations
remain unknown until actual conditions finish.

The model is `Qwen/Qwen3-Coder-30B-A3B-Instruct`, revision
`b2cff646eb4bb1d68355c01b18ae02e7cf42d120`. Actual tokenizer, chat-template and
config bytes must match the retained successful model-verification report.
The historical three-byte `{}` tokenizer files are rejected. The builder
renders the pinned template for two text roles without tools, verifies that
rendering, and measures exactly 512/60,000 tokens using the real tokenizer.
It retains rendered prompt bytes, counts and tokenizer/library hashes/versions.
The request policy is `temperature=0`, `top_p=1`, `seed=0`, `n=1`,
`stream=false`, `max_tokens=128`, `ignore_eos=true`. Native response usage must
still confirm actual prompt and output counts; deterministic output is not
assumed from those settings.

Production prefix caching is preserved. The inspected vLLM 0.10.0 source
defaults generation to prefix caching in V1. Its `/reset_prefix_cache` route
returns HTTP 200 without checking whether the cache was successfully reset.
The **cold-reset recipe awaits main review and model-adapter integration**:

1. Reserve the endpoint; independently label and retain one
   `POST /reset_prefix_cache` before every model condition. No model warmup.
2. Retain the reset response and server observer ordering/completeness proof;
   disallow intervening inference. Charge reset/setup cost to startup.
3. Dispatch the unchanged measured request. Require native prompt tokens of
   512/60,000, native `cached_tokens == 0`, and 128 output tokens on each side.
   Missing, ambiguous, nonzero-cache, or unequal counts invalidate the pair.

The recipe is in `cache_reset_recipe.json`; no reset or inference is performed
by the builder. The raw response usage, reporting-only
`--enable-prompt-tokens-details`, native journal attribution, and deferred
observer joins belong to the pending model integration. Fixture creation does
not set a model full-capture flag or claim the live server setting is verified.

CPU sidecars require the separately owned, source-hashed CPU policy. The adapter
uses that owner's control affinity and Docker cpuset helpers and verifies the
daemon configuration plus actual host-container process affinity. Worker 00
uses logical CPU 0; controls/collectors use `11-15,27-31`. The 16-core/32-thread
SMT topology is verified by the existing policy code; no quota or placement
algorithm is introduced here. A short temporary BPF socket path permits durable
output beneath deep submission directories; socket cleanup remains charged to
work, along with service and Docker teardown.

The active bundle is the submission's
`verification/fixed-work-fixtures-20260909T022827Z/`. Its manifest SHA-256 is
`389ccd2dc090d563bed7cd48f4ae3918d33da33c2ff6587cf71600b5c06dad68`.
All four workload and snapshot bytes remain the same as the first materialized
bundle; runtime placement and cache-policy revisions do not resize the work.

To collect only CPU fixtures, use the builder's `cpu-subset` command with the
bundle manifest, sealed `runtime/worker-00.json` and its SHA-256, and the
policy owner's successful placement proof. It runs exactly six pairs/twelve
conditions through the existing runner reset/pair functions, stops after an
invalid pair, retains actual timings and capture, and always reports
`threshold_status: not_evaluated_cpu_subset`. It never launches model fixtures
or labels two CPU medians as the four-fixture gate. Runtime source changes
invalidate the subset proof.

The historical placement reference may predate the current policy source; both
hashes and that mismatch remain explicit. Current policy topology and control
placement are checked live, and every condition must retain its own measured
Docker configuration and host-process affinity matching the sealed runtime.
An older smoke is never relabelled as proof of the current source.

The completed sibling `fixed-work-fixtures-20260909T022827Z-cpu-subset/`
retains twelve conditions and six valid pairs with unchanged runtime sources.
All on conditions passed full hook/BPF/cgroup validation with zero loss or map
failures. Every condition verified worker CPU 0 and the control pool. Results:

| Fixture | Median off work ms | Median on work ms | Median paired overhead | Individual CPU records per on condition |
|---|---:|---:|---:|---:|
| File/traversal | 630.317 | 1,034.770 | 64.17% | 1,230 |
| Test/script/subprocess | 1,358.173 | 2,338.585 | 68.50% | 4,001 |

Paired overhead is the median of the three pair ratios, not the ratio of the
two timing medians. Work includes completion/teardown; startup is retained
separately (roughly 2.9 seconds off and 4.8–5.2 seconds on). These whole-fixture
results miss the overhead targets. No model condition or cache reset was sent,
and the four-fixture gate remains unevaluated. The fixed workload sizes are
unchanged. Earlier attempts are retained: one stopped before launch on a stale
reference-proof source hash; another exposed two startup integration defects
before fixture action dispatch. Those defects were fixed in the adapter's
wrapped Docker inspection and explicit-hook launcher, then the subset completed.

Each bundle's `commands.json` supplies validation-only, CPU-subset, and eventual
full 24-condition argv. Full model execution still requires main's cache-recipe
review, observed server configuration, native/deferred serving attribution,
and full model hook capture. `serving_metrics_config` is an explicit external
runtime path, not a fabricated witness or server identity.
