# CPU-Docker / H100 Collection Requirements

This is the supported execution model for parallel H100 collection when the
PACE compute nodes do not provide a usable container runtime:

- The CPU VM runs the six SWE-agent case runners through its Docker daemon.
- Each runner sends model requests through its own proxy and SSH tunnel to one
  H100 allocation's local vLLM endpoint.
- The H100 allocations provide inference only. They must not be used as
  shared runner or container hosts.
- Do not use the CPU VM for GPU probes, CUDA measurements, or claims that the
  case runner itself executed on an H100.

## Required isolation

Every worker must have all of the following:

| Resource | Requirement |
|---|---|
| Case | One distinct case and distinct resume key |
| H100 endpoint | One worker-specific tunnel, for example `127.0.0.1:18000` through `18005` |
| Request proxy | One unique local listen port, for example `20000` through `20005` |
| Container runtime | CPU VM Docker, with one worker-specific container/output namespace |
| Cache | A separate cache directory |
| Output | A separate immutable output directory |

Workers run concurrently. Do not serialize workers that happen to share a
compute node; their vLLM endpoints, proxy ports, Docker containers, caches,
and output roots must remain distinct.

## Before a run

1. Confirm six Slurm allocations are `RUNNING`, each requests exactly one H100.
2. Confirm each worker tunnel reaches `/v1/models` and reports the pinned model.
3. Confirm the CPU VM can access Docker without an interactive password.
4. Confirm proxy ports and output roots are unused.
5. Confirm the case files contain six distinct `instance_id` and `resume_key`
   values.
6. Keep the pinned model, SWE-agent, SWE-bench, tokenizer, and repository
   revisions unchanged.

For PACE control commands, use the authenticated control socket and numeric
login relay:

```bash
PACE_SSH='ssh -S /home/jasonrivera691/.ssh/cm/pace-control \
  -o ControlMaster=no -o BatchMode=yes \
  jriverah3@128.61.254.151'
```

Do not use an unresolved PACE hostname, key-only authentication, or
`~/.ssh/id_rsa`.

## Launch pattern

The exact paths and case IDs are deployment-specific. The important pattern is
one proxy and one runner per worker:

```bash
for worker in 00 01 02 03 04 05; do
  model_port=$((18000 + 10#$worker))
  proxy_port=$((20000 + 10#$worker))
  case_root="/mnt/eic-work/assignment/cpu-docker-canary/worker-$worker"

  python3 scripts/observability/request_proxy.py \
    --listen-host 127.0.0.1 \
    --listen-port "$proxy_port" \
    --upstream-host 127.0.0.1 \
    --upstream-port "$model_port" \
    --events "$case_root/request_proxy.jsonl" \
    >"$case_root/proxy.log" 2>&1 &

  sudo -n env HOME="$HOME" \
    EIC_MODEL_API_BASE="http://127.0.0.1:$proxy_port/v1" \
    VLLM_METRICS_URL="http://127.0.0.1:$model_port/metrics" \
    /mnt/eic-work/repos/SWE-agent/.venv/bin/sweagent run-batch \
    --config /mnt/eic-work/repos/SWE-agent/config/default.yaml \
    --config /mnt/eic-work/repos/agentic-workload-simulator/cloud/lambda/sweagent_request.yaml \
    --instances.type file \
    --instances.path "$case_root/instances.json" \
    --instances.filter '.*' \
    --agent.model.api_base "http://127.0.0.1:$proxy_port/v1" \
    --agent.model.api_key EMPTY \
    --agent.model.total_cost_limit 0 \
    --agent.model.per_instance_cost_limit 0 \
    --agent.model.per_instance_call_limit 30 \
    --agent.model.temperature 0.0 \
    --agent.model.max_input_tokens 32768 \
    --agent.model.max_output_tokens 2048 \
    --agent.tools.parse_function.type thought_action \
    --output_dir "$case_root/run" \
    --num_workers 1 \
    >"$case_root/runner.log" 2>&1 &
done
```

The direct `run-batch` command is useful only for a transport smoke test. It
does not produce the assignment `case_result.json` contract by itself. For a
measured assignment case, create a plan-derived `case_spec.json` and invoke
the reviewed matrix path with the explicit CPU-Docker mode:

```bash
python3 scripts/assignment/run_matrix.py \
  --plan /absolute/path/worker-plan.jsonl \
  --sha256-sidecar /absolute/path/worker-plan.jsonl.sha256 \
  --runner /absolute/path/scripts/assignment/sweagent_case_runner.py \
  --runtime-manifest /absolute/path/runtime-manifest.json \
  --output-dir /absolute/path/output/worker-00 \
  --execute --acknowledge-paid-gpu-work \
  --cpu-docker --max-cases 1
```

`--cpu-docker` records `cpu-docker-runner+h100-inference`, skips the
H100 probe on the CPU VM, and still requires the pinned manifest, proxy,
evaluator, hashes, and case-result contract. Do not add
`--instances.deployment.type local`: the CPU Docker daemon must create the
SWE-bench environment containers.

## Verify and stop

For each worker, verify:

- the runner log identifies the intended case;
- the proxy log shows the expected listen port and upstream tunnel;
- the Docker container was created and removed by the runner;
- `request_proxy.jsonl` contains successful measured requests;
- the output contains exactly one trajectory and no unrelated cases.

Stop after the six canaries. Do not begin the full matrix automatically.
Preserve failed outputs and logs; use a new output root for every rerun.

## Common failures

- **PACE runner says Docker permission denied:** use the CPU-Docker model
  above; do not attempt to repair worker services.
- **Rootless Podman has no subuid/subgid range:** do not use Podman as a
  workaround unless PACE administrators configure it.
- **Proxy address already in use:** identify the worker owning the port and
  choose a new worker-specific port. Never share a proxy port.
- **Tunnel `/v1/models` fails:** stop before launching the runner and repair the
  tunnel. A healthy Slurm allocation alone is insufficient.
- **Duplicate case or resume key:** stop and regenerate the six case inputs.
