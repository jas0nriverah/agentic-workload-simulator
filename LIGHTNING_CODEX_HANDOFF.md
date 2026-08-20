# Lightning hands-on handoff

This is the execution handoff for a second Codex operating inside the
already-created Lightning Studio. Read it completely before running a paid
command.

## Objective

Finish the provider-specific Linux x86-64 rehearsal and, only after every gate
passes, run the first official SWE-agent control trajectory required by the EIC
assignment. The assignment PDF and the frozen repository plan remain the
sources of truth. Do not redesign the experiment, change the model, start the
four sweeps, or invent a result.

The first session is deliberately narrow:

1. preflight and pinned bootstrap;
2. vLLM health, including a parsed qwen3_coder tool call and /metrics;
3. Lite and Verified gold-patch smokes;
4. one uninstrumented Lite SWE-agent trajectory;
5. official generated-patch evaluation and lossless artifact inventory;
6. export/checksum, stop workloads, and stop the Studio.

Do not run thin telemetry, Nsight, strace, calibration, or any sweep until the
raw first .traj fixture has been reviewed locally.

## Current repository state

The coordinator has implemented and locally tested:

- Lightning manifest rendering with managed-Conda support;
- cache/path propagation for the vLLM container;
- authenticated Docker /v2/ reachability handling;
- evaluator digest checks using Docker's repository@sha256 form;
- Lightning session-provider handling;
- utility installation before blocking preflight;
- Python lock-path selection and Python-version/header validation;
- resume markers bound to a bootstrap fingerprint (manifest, code, lock,
  environment mode/root, version, and work root);
- managed-environment pip check and a recorded python-freeze.txt.

The exact coordinator commit is the commit containing this file. Confirm it
before proceeding:

~~~bash
cd /teamspace/studios/this_studio/agentic-workload-simulator
git rev-parse HEAD
git status --short
~~~

The Studio has one managed Conda environment and rejects python3 -m venv.
The canonical Lambda path still uses a fresh Python 3.11 venv. The Studio
renderer records the actual managed interpreter and rewrites every command
contract to that interpreter.

## Non-negotiable safety rules

- Do not launch another GPU or provider session. Use the existing Studio only.
- Do not put a real token in a manifest, YAML session file, Git commit, log, or
  artifact. The SWE-agent wrapper only needs a process-local value such as
  export VLLM_API_KEY=local-only-key.
- Do not bypass a failed preflight, lock check, image digest check, healthcheck,
  evaluator check, or session gate.
- Do not use --skip-model-download for the official trajectory.
- Do not call an unofficial evaluator or substitute a public score for the
  official report.
- If a command fails, preserve its log and report the exact failure; do not
  fabricate a PASS or edit an artifact into existence.
- Keep the Studio stopped/sleeping whenever you are not actively running a
  required command. Record the visible credit balance before and after the
  first session and stop at the user/provider cap.

## Step 1 — synchronize and inspect

The cloud checkout may currently be on a local main branch. Fast-forward it to
the reviewed branch without resetting or discarding files:

~~~bash
cd /teamspace/studios/this_studio/agentic-workload-simulator
git fetch origin pre-h100-hardening
git merge --ff-only FETCH_HEAD
git rev-parse HEAD
git status --short
~~~

The only expected untracked files are provider-local manifests/reports. Do not
commit cloud/lambda/instance_manifest.env or any session authorization file.

Confirm the host before any bootstrap:

~~~bash
uname -m
python3 --version
python3 -c 'import sys,sysconfig; print(sys.executable); print(sys.prefix); print(sysconfig.get_platform())'
nvidia-smi
docker info --format '{{.ServerVersion}}'
df -h /
~~~

Expected: x86_64, the active managed Python (observed Studio was 3.12.11),
one idle H100 80 GB, working Docker, and at least 120 GiB free. A missing GPU
or a busy GPU is a stop condition.

## Step 2 — authorize the paid session before provider work

Before any command that can install from the network, download a model/image, or
run a billed workload, require an explicit user-filled Lightning authorization
file. Do not invent its cap or UTC window. If it is absent or unauthorized,
stop and report that the user must fill it.

~~~bash
test -f cloud/lightning/cloud_session.yaml || cp cloud/lightning/cloud_session.yaml.example cloud/lightning/cloud_session.yaml
$EDITOR cloud/lightning/cloud_session.yaml
scripts/cloud/lambda_session_gate.sh \
  --session cloud/lightning/cloud_session.yaml --gate G3A
~~~

The gate is local-only: it does not launch or terminate a Studio. It must pass
before lock resolution, real bootstrap, or the first trajectory. Re-run it
immediately before starting vLLM because the authorization window may expire
while bootstrap is running.

## Step 3 — resolve the managed Python lock before billing work

The tracked host lock is explicitly resolved for Python 3.11. The Studio's
managed Python is normally 3.12. The bootstrap now rejects pairing those two;
this is intentional. Do not relabel the 3.11 lock.

If the matching provider lock is not already present, generate it from the
same pinned set on Linux x86-64 (the resolver does not download the model):

~~~bash
cd /teamspace/studios/this_studio/agentic-workload-simulator
mkdir -p /teamspace/studios/this_studio/agentic-work/artifacts/manifests
if ! command -v uv >/dev/null 2>&1; then
  python3 -m pip install --user uv
fi
export PATH="$(python3 -m site --user-base)/bin:$PATH"
command -v uv >/dev/null 2>&1 || { echo 'uv is unavailable after installation' >&2; exit 1; }
uv --version | tee /teamspace/studios/this_studio/agentic-work/artifacts/manifests/uv-version.txt
uv pip compile cloud/lambda/requirements-linux-x86_64.txt \
  --python-version 3.12 \
  --python-platform x86_64-manylinux2014 \
  --resolution highest --generate-hashes \
  --output-file cloud/lambda/requirements-linux-x86_64-py312.txt
head -n 5 cloud/lambda/requirements-linux-x86_64-py312.txt
head -n 5 cloud/lambda/requirements-linux-x86_64-py312.txt | grep -F -- '--python-version 3.12'
head -n 5 cloud/lambda/requirements-linux-x86_64-py312.txt | grep -F -- '--python-platform x86_64-manylinux2014'
sha256sum cloud/lambda/requirements-linux-x86_64-py312.txt
~~~

If uv pip compile rejects the existing lock as an input, inspect the pinned
SWE-agent and SWE-bench pyproject.toml files and reproduce the same base
dependency set; keep all versions/hash pins and document the resolver command.
The output must be a real hash lock with the two header assertions above. If
the resolver cannot produce one, stop before lambda_bootstrap.sh and report
the unresolved Python-3.12 compatibility blocker.

## Step 4 — render and validate the Studio manifest

Run this after the lock exists. The renderer automatically selects
requirements-linux-x86_64-py312.txt when it is present and records its SHA.

~~~bash
scripts/cloud/render_studio_manifest.sh \
  --output cloud/lambda/instance_manifest.env \
  --studio-root /teamspace/studios/this_studio --force

grep -E '^(PROJECT_ROOT|WORK_ROOT|CACHE_ROOT|PYTHON_VERSION|PYTHON_VERSION_EXACT|PYTHON_ENV_|PYTHON_LOCK_PATH|PYTHON_LOCK_SHA256|EVALUATOR_PYTHON)=' \
  cloud/lambda/instance_manifest.env
! grep -E '/home/ubuntu|/agentic-work/source|/agentic-work/venv' cloud/lambda/instance_manifest.env
~~~

For a managed 3.12 Studio, PYTHON_LOCK_PATH must end in
requirements-linux-x86_64-py312.txt; otherwise stop and resolve the lock.
The command fields must point at the managed prefix, not
/teamspace/.../agentic-work/venv.

## Step 5 — preflight, dry-run, and bootstrap

Keep a persistent log. The first invocation after this patch may rerun old
stages because the fingerprint intentionally invalidates timestamp-only
markers.

~~~bash
mkdir -p /teamspace/studios/this_studio/agentic-work/artifacts/manifests
scripts/cloud/lambda_preflight.sh \
  --manifest cloud/lambda/instance_manifest.env \
  --output /teamspace/studios/this_studio/agentic-work/artifacts/manifests/lightning_preflight.json
scripts/cloud/lambda_bootstrap.sh \
  --manifest cloud/lambda/instance_manifest.env --dry-run

tmux new -A -s eic-bootstrap
cd /teamspace/studios/this_studio/agentic-workload-simulator
scripts/cloud/lambda_bootstrap.sh \
  --manifest cloud/lambda/instance_manifest.env --resume
~~~

The real bootstrap may install the hash-locked host dependencies, clone the
detached SWE-agent/SWE-bench revisions, download the pinned Qwen snapshot,
pull/verify the three evaluator images, and run the local tests. It must finish
with bootstrap.json, python-freeze.txt, and all stage markers present. The
bootstrap JSON must report the actual managed Python version and selected lock
SHA.

## Step 6 — runtime and first official control

Only continue if preflight and bootstrap both pass. Use a process-local API
key; it is not a provider credential:

~~~bash
scripts/cloud/lambda_session_gate.sh \
  --session cloud/lightning/cloud_session.yaml --gate G3A
export VLLM_API_KEY=local-only-key
scripts/cloud/lambda_start_vllm.sh --manifest cloud/lambda/instance_manifest.env
tail -n 100 /teamspace/studios/this_studio/agentic-work/logs/vllm/server.log
scripts/cloud/lambda_healthcheck.sh \
  --manifest cloud/lambda/instance_manifest.env \
  --work-root /teamspace/studios/this_studio/agentic-work
scripts/cloud/lambda_run_gold_smoke.sh --manifest cloud/lambda/instance_manifest.env --suite lite
scripts/cloud/lambda_run_gold_smoke.sh --manifest cloud/lambda/instance_manifest.env --suite verified
scripts/cloud/lambda_run_first_experiment.sh \
  --manifest cloud/lambda/instance_manifest.env \
  --instance-id astropy__astropy-12907 \
  --experiment-id first-lite-astropy__astropy-12907 \
  --mode uninstrumented
~~~

The healthcheck is a hard gate: /v1/models, normal completion, parsed
qwen3_coder tool call, required native /metrics families, and an H100 sample
must all pass. The two gold smokes must use their pinned evaluator digests.
The first trajectory remains uninstrumented; do not run thin mode in this
session.

## Step 7 — export, inventory, and stop

Preserve raw files byte-for-byte and record checksums before stopping the
Studio. Use the repository's collection/inventory tools; do not normalize a
.traj yet.

~~~bash
scripts/cloud/lambda_collect_results.sh \
  --source-root /teamspace/studios/this_studio/agentic-work/data/raw/first-lite-astropy__astropy-12907 \
  --output-dir /teamspace/studios/this_studio/agentic-work/export \
  --run-id first-lite-astropy__astropy-12907-control
python3 scripts/validation/inventory_sweagent_output.py \
  --root /teamspace/studios/this_studio/agentic-work/experiments/first-lite-astropy__astropy-12907 \
  --output /teamspace/studios/this_studio/agentic-work/artifacts/manifests/first-trajectory-inventory.json
scripts/cloud/lambda_stop_workloads.sh \
  --work-root /teamspace/studios/this_studio/agentic-work \
  --server-manifest /teamspace/studios/this_studio/agentic-work/artifacts/manifests/vllm_server.json \
  --session vllm-agentic \
  --gpu-lock-dir /teamspace/studios/this_studio/agentic-work/locks/gpu-0.lock
~~~

Verify the exported checksums, copy the export off the Studio, then stop the
Studio in the Lightning UI. Do not leave a billed H100 running while waiting
for review.

## Completion report to return

Return exact command output paths, commit/lock hashes, runtime version, credit
usage, and these fields. Never fill a field with an estimate or invented
result:

~~~text
LINUX PREFLIGHT: PASS | BLOCKED
PYTHON LOCK: PASS | BLOCKED (path + SHA-256)
BOOTSTRAP: PASS | BLOCKED
VLLM HEALTH: PASS | BLOCKED
LITE GOLD SMOKE: PASS | BLOCKED
VERIFIED GOLD SMOKE: PASS | BLOCKED
FIRST CONTROL TRAJECTORY: PASS | BLOCKED
OFFICIAL EVALUATION: RESOLVED | UNRESOLVED | ERROR
ARTIFACT EXPORT: PASS | BLOCKED
H100 CREDITS USED:
H100-ONLY UNCERTAINTIES:
STUDIO STOPPED: YES | NO
~~~

If the Python 3.12 lock cannot be resolved, or any hard gate fails, return
BLOCKED with the exact log and do not start vLLM or the SWE-agent trajectory.
