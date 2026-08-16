# Cloud-readiness interface handoff

Status: frozen after CR4; implementation workers must consume this contract.

## Immutable runtime

- Model: `Qwen/Qwen3-Coder-30B-A3B-Instruct`
- Model revision: `b2cff646eb4bb1d68355c01b18ae02e7cf42d120`
- Precision: BF16; health context 8192; experiment context 32768
- vLLM: `vllm/vllm-openai:v0.10.0@sha256:05a31dc4185b042e91f4d2183689ac8a87bd845713d5c3f987563c5899878271`
- vLLM source revision: `6d8d0a24c02bfd84d46b3016b865a44f048ae84b`
- Tool parser: `qwen3_coder`; metrics: `GET http://127.0.0.1:8000/metrics`
- SWE-agent: v1.1.0 commit `0f3acafacabc0def8cc76b4e48acb4b6cf302cb9`
- SWE-bench harness: v4.1.0 commit `726c5461e2ef52d83cf1ea2107870a8bb3328d57`

## Dataset and IDs

- Lite: `SWE-bench/SWE-bench_Lite@69611d31007e1c6731db8bd5b5c3f2d33f5bab6e`,
  300 rows, first real `astropy__astropy-12907`, gold smoke
  `astropy__astropy-14182`.
- Verified: `SWE-bench/SWE-bench_Verified@91aa3ed51b709be6457e12d00300a6a596d4c6a3`,
  500 rows, gold smoke `astropy__astropy-14365`.
- Candidate representation hashes are in `first_experiment.yaml`; a Linux
  download must record the exact file/content hash before empirical use.

## Commands and boundaries

The runner invokes the pinned SWE-agent directly with a local OpenAI-compatible
endpoint (`http://127.0.0.1:8000/v1`), provider-prefixed served model, zero
cost limits, temperature 0, max 30 steps, max input 32768, max output 2048,
and one worker. `uninstrumented` is the control. `thin-telemetry` may only
capture approved event/Prometheus/GPU samples and must not mutate requests.

Official evaluator invocation is `python -m swebench.harness.run_evaluation`
with a local pinned dataset JSON/JSONL or the recorded revision, `--split test`,
the exact instance ID, generated prediction path, `--max_workers 1`, explicit
run ID, and a separate output/report directory. Gold uses `--predictions_path
gold` and separate suite output paths. Evaluator wall time is not trajectory
E2E. Gold-smoke artifacts use `GOLD_OUTPUT_ROOT` and `gold-*` run IDs. The
optional generated-evaluation mode requires `model_name_or_path` in the
prediction file and uses `GENERATED_OUTPUT_ROOT` with `generated-*` run IDs;
the two namespaces are never reused.

## Artifact contract

Each attempt writes an immutable directory containing `config.json`,
`events.jsonl`, `model_calls.jsonl`, `tool_calls.jsonl`, `prediction.json`,
`eval.json`, `summary.json`, referenced stdout/stderr logs, and an explicit
`status`/`provenance` for unavailable measurements. Retry attempts are named
and append-only. A run-level manifest records command/config hashes, revisions,
IDs, host clocks, metrics references, and evaluator handoff.

## Required environment

The source of truth for launch values is the untracked instance manifest copied
from `cloud/lambda/instance_manifest.env.example`. Runtime scripts must reject
missing or contradictory immutable fields. User-only values (instance address,
price, paid-session caps, stop/export/termination times, and credentials) do
not belong in this repository.
