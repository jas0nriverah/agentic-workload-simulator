# Expected Lambda environment

This file records the reviewed target and acceptance checks. It is not a
claim about the current laptop or any unlaunched VM.

## Host acceptance

- Ubuntu/Linux x86-64 (`uname -m` must be `x86_64`);
- exactly one isolated NVIDIA GPU with at least 80,000 MiB VRAM and compute
  capability 9.0 or newer (the requested SKU is one H100 PCIe 80 GB);
- Docker daemon, `nvidia-container-toolkit`, and `nvidia-smi` available;
- at least 120 GiB free on the work filesystem before model download;
- `git`, `curl`, `jq`, `rsync`, `tmux`, `tar`, `zstd`, Python 3.11+, and an
  outbound connection to GitHub, Hugging Face, PyPI, and Docker Hub;
- no unrelated GPU process and no listener on TCP port 8000.

The preflight report must record the actual OS, kernel, GPU name/VRAM/count,
compute capability, driver/CUDA, Docker, network, filesystem, and CPU/RAM.
Any materially different billable SKU or precision requires a new decision
record before work starts.

## Pinned runtime

- Model: `Qwen/Qwen3-Coder-30B-A3B-Instruct`, revision
  `b2cff646eb4bb1d68355c01b18ae02e7cf42d120`, BF16;
- vLLM source revision `6d8d0a24c02bfd84d46b3016b865a44f048ae84b`, version
  `0.10.0`, image
  `vllm/vllm-openai:v0.10.0@sha256:05a31dc4185b042e91f4d2183689ac8a87bd845713d5c3f987563c5899878271`;
- parser `qwen3_coder`, maximum model length 32768, tensor parallel size 1,
  GPU memory utilization 0.90, localhost port 8000;
- SWE-agent v1.1.0 revision
  `0f3acafacabc0def8cc76b4e48acb4b6cf302cb9`;
- SWE-bench v4.1.0 revision
  `726c5461e2ef52d83cf1ea2107870a8bb3328d57`;
- host-side Python dependencies are constrained by
  `cloud/lambda/requirements-linux-x86_64.txt` (SHA-256
  `7e1177bf4c0b4efe4d64895f39b340413336b77e02d2f72bbf5aad387accc9cc`),
  resolved for Python 3.11/Linux x86-64 and installed with pip
  `--require-hashes`; the detached SWE-agent/SWE-bench packages are installed
  with `--no-deps` after that lock is applied;
- Lite dataset revision
  `69611d31007e1c6731db8bd5b5c3f2d33f5bab6e` (300 test rows);
- Verified dataset revision
  `91aa3ed51b709be6457e12d00300a6a596d4c6a3` (500 test rows).

The selected local task files are `lite_astropy__astropy-12907.json`,
`lite_astropy__astropy-14182.json`, and
`verified_astropy__astropy-14365.json` under
`/home/ubuntu/agentic-work/datasets`. Their selected-row hashes are recorded
in `datasets.json` after the pinned download; the full-representation hashes
remain in `instance_manifest.env.example` and `first_experiment.yaml`.

## Runtime evidence required on H100

The host must produce all of the following before the first trajectory is
accepted: a successful `/v1/models` response, one normal completion, one
`qwen3_coder` parsed tool call, native `/metrics` values for
`vllm:request_success_total`, `vllm:prompt_tokens_total`,
`vllm:generation_tokens_total`, and
`vllm:e2e_request_latency_seconds`, and a GPU sample. The official evaluator
must inspect the three pinned `linux/amd64` image digests and emit a valid
one-instance report.

The current development environment is macOS arm64 without CUDA/Docker; it
cannot prove these host-only conditions. No H100 empirical result is claimed
until the recorded reports exist.
