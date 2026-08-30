# CPU controller to PACE vLLM tunnel runbook

This runbook records the working transport used to let the CPU-side
assignment controller call a vLLM server on a PACE H100. It is intended for
repeated runs. The vLLM process, PACE relay, VPN, and CPU-side forward can be
kept alive across multiple serialized assignment runs; only run-specific
artifacts and metric snapshots should change.

This is a transport runbook. It does not authorize a workload, change the
assignment plan, or replace the H100 repository-profiling and completion
runbooks.

## Topology

The validated path has two SSH forwards and one VPN:

```text
CPU controller: 127.0.0.1:18000
       │  SSH -L, initiated on the CPU VM
       │  over the Georgia Tech GlobalProtect VPN
       ▼
PACE ICE login relay: 127.0.0.1:18000
       │  SSH -R, initiated on the PACE compute node
       ▼
PACE H100 compute node: 127.0.0.1:8000 (vLLM)
```

The PACE compute node does not need to be directly resolvable or reachable
from the CPU VM. The PACE-side reverse relay targets compute-node loopback;
the CPU-side forward targets the login relay's loopback. vLLM stays bound to
loopback and is never exposed through a public firewall rule.

The data path is encrypted as follows:

- CPU VM to the Georgia Tech VPN gateway: GlobalProtect TLS/ESP;
- CPU VM to the PACE login relay: SSH inside the VPN;
- PACE compute node to the PACE login relay: SSH;
- vLLM HTTP itself: loopback-only HTTP, protected by the two encrypted
  transport layers before it leaves either host.

## Validated configuration

Replace these values when the allocation or VM changes:

| Role | Validated value |
| --- | --- |
| PACE vLLM bind | `127.0.0.1:8000` |
| PACE model | `Qwen/Qwen3-Coder-30B-A3B-Instruct` |
| Model revision | `b2cff646eb4bb1d68355c01b18ae02e7cf42d120` |
| vLLM | `0.10.0` |
| PACE relay 1 | `login-ice-gnr-1.pace.gatech.edu` / `128.61.254.151` |
| PACE relay 2 | `login-ice-gnr-2.pace.gatech.edu` / `128.61.254.154` |
| CPU-side controller endpoint | `http://127.0.0.1:18000/v1` |
| PACE-side vLLM endpoint | `http://127.0.0.1:8000/v1` |

The numeric relay addresses were used from the CPU VM because the ephemeral
PACE compute hostname was not resolvable there. Reconfirm relay DNS and host
keys through the approved PACE channel before trusting a changed address.

## One-time CPU VM setup

The CPU VM is an Ubuntu 22.04 Google Cloud VM. Run GCP management commands
from Cloud Shell, not from inside the VM. The VM's restricted service-account
scopes do not permit `gcloud compute ssh` to manage itself.

### Install and verify OpenConnect

The Ubuntu package in the validated session was OpenConnect 8.20. The
GlobalProtect/Duo flow was reliable with the signed OpenConnect 9.21 build
installed at `/usr/local/sbin/openconnect`.

Verify an existing setup first:

```bash
/usr/local/sbin/openconnect --version
```

If it is not present, install the build dependencies and build from the
official signed source archive. Verify the detached signature before unpacking
or compiling; the signing-key fingerprint used in the validated setup was:

```text
BE07 D9FD 5480 9AB2 C4B0 FF5F 6376 2CDA 67E2 F359
```

The build needs `gettext`; without `msgfmt`, `./configure` stops before
creating a Makefile. The installed binary is `/usr/local/sbin/openconnect`,
not `/usr/local/bin/openconnect`.

### Prepare an SSH authentication method for PACE

The validated CPU session authenticated to the PACE login relay with the PACE
password. No private key belongs in this repository, a manifest, shell
history, or chat. If an approved key is used later, pass its actual path with
`-i`; do not assume `~/.ssh/id_rsa` exists.

## Per-session startup

Use separate persistent sessions for the PACE server, the VPN, and the CPU
controller shell. Do not close the VPN session while runs are active.

### 1. Start or verify vLLM on PACE

Run this on the allocated PACE compute node, not on the login host. If the
server is already healthy and has the required pins, reuse it rather than
starting a second server.

The validated direct launch used these material settings:

```bash
pace_user=jriverah3
pace_root=/storage/ice1/9/6/"$pace_user"/eic-work
server_dir="$pace_root/runtime/pace-vllm-server"
mkdir -p "$server_dir"

"$pace_root/vllm-venv/bin/python" -m vllm.entrypoints.openai.api_server \
  --model "$pace_root/models/Qwen3-Coder-30B-A3B-Instruct" \
  --revision b2cff646eb4bb1d68355c01b18ae02e7cf42d120 \
  --served-model-name Qwen/Qwen3-Coder-30B-A3B-Instruct \
  --host 127.0.0.1 \
  --port 8000 \
  --dtype bfloat16 \
  --max-model-len 32768 \
  --gpu-memory-utilization 0.90 \
  --tensor-parallel-size 1 \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_coder \
  2>&1 | tee -a "$server_dir/server.log"
```

Use the repository's reviewed H100 launcher or the allocation's persistent
job wrapper when available; the command above documents the server contract,
not an instruction to bypass scheduler policy.

Verify locally on PACE:

```bash
curl -sS -o /dev/null -w 'health_http=%{http_code}\n' http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/v1/models
curl -fsS http://127.0.0.1:8000/metrics >/dev/null && echo metrics-ok
```

Require `health_http=200`, the expected model ID and revision, and
`metrics-ok` before creating a relay.

### 2. Create the PACE-side reverse relay

From the PACE compute node, keep this command running in a persistent job or
terminal. The remote login host receives loopback port 18000 and forwards it
back to the compute node's vLLM loopback port 8000:

```bash
ssh -N -T \
  -o BatchMode=yes \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=60 \
  -o ServerAliveCountMax=3 \
  -R 127.0.0.1:18000:127.0.0.1:8000 \
  jriverah3@login-ice-gnr-1.pace.gatech.edu
```

If relay 1 is unavailable, start the same command to
`login-ice-gnr-2.pace.gatech.edu` and use `128.61.254.154` for the CPU-side
forward below. Two independent relay processes may be kept as failover, one
per login host; do not start duplicate relays on the same login host and port.

Optional PACE-side relay verification, run from the compute node:

```bash
ssh jriverah3@login-ice-gnr-1.pace.gatech.edu \
  'curl -sS -o /dev/null -w "relay_health_http=%{http_code}\n" http://127.0.0.1:18000/health'
ssh jriverah3@login-ice-gnr-1.pace.gatech.edu \
  'curl -fsS http://127.0.0.1:18000/v1/models'
```

### 3. Connect the CPU VM to the Georgia Tech VPN

From a CPU VM shell, first preserve the Google Cloud IAP and metadata routes.
The exact current gateway is `10.128.0.1` on `ens4`; the discovery form below
also works on a replacement VM:

```bash
gcp_gateway="$(ip route show default | awk 'NR==1 {print $3}')"
gcp_device="$(ip route show default | awk 'NR==1 {print $5}')"
sudo ip route replace 35.235.240.0/20 via "$gcp_gateway" dev "$gcp_device"
sudo ip route replace 169.254.169.254/32 via "$gcp_gateway" dev "$gcp_device"
```

Start OpenConnect in its own persistent CPU VM terminal:

```bash
sudo /usr/local/sbin/openconnect \
  --protocol=gp \
  --user=jriverah3 \
  https://vpn.gatech.edu
```

Complete the password and Duo prompts, then select `DC Gateway`. The
successful end state includes:

```text
ESP tunnel connected
Configured as 10.2.x.x ...
Using vhost-net for tun acceleration
```

The process intentionally occupies the terminal. Do not press `Ctrl-C` after
`ESP tunnel connected`; that logs out and removes the VPN interface. Open a
second Cloud Shell tab for the next step. If tmux is used, create the session
only once and reattach to it rather than nesting another `tmux new`.

Do not use `--no-routes` with the validated OpenConnect 9.21 binary; that
option is not supported. The explicit GCP route exceptions above are the
working method for keeping IAP access alive.

### 4. Open a second CPU VM shell through IAP

In Cloud Shell, run this command. Do not run it from inside the VM:

```bash
gcloud compute ssh instance-20260826-163710 \
  --zone=us-central1-a \
  --project=project-3d59272d-3213-4e06-97b \
  --tunnel-through-iap
```

Once the prompt is `jasonrivera691@instance-...`, verify the VPN and PACE
relay reachability:

```bash
ip -br link | grep -E 'tun|ppp' || echo 'no VPN interface'
nc -vz -w 15 128.61.254.151 22
```

Require a `tun0`-like interface and a successful TCP connection before trying
SSH. If the IAP connection itself returns `insufficient authentication
scopes`, the command was run inside the VM; return to Cloud Shell and run it
there.

### 5. Test PACE SSH authentication

Use an interactive test once per VM or once per newly selected relay:

```bash
ssh -o ConnectTimeout=15 jriverah3@128.61.254.151 'echo pace-ssh-ok'
```

On first connection, verify the host-key fingerprint through the approved
PACE channel and then accept it. At the password prompt, enter the PACE
password; it is intentionally invisible. Do not use the GCP password and do
not paste credentials into logs or chat.

The expected result is:

```text
pace-ssh-ok
```

### 6. Create the CPU-side local forward

The validated session used password authentication, so this command omits a
missing `id_rsa` path and `BatchMode`:

```bash
ssh -fNT \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=60 \
  -o ServerAliveCountMax=3 \
  -L 127.0.0.1:18000:127.0.0.1:18000 \
  jriverah3@128.61.254.151
```

Enter the PACE password. With `-fNT`, a return to the shell prompt and no
output means the SSH process has been backgrounded. The tunnel is not the
same as the VPN: both must remain alive.

### 7. Verify the CPU-local endpoint

Run all four checks from the CPU VM shell:

```bash
ss -ltn | grep ':18000'
curl -sS -o /dev/null -w 'health_http=%{http_code}\n' \
  http://127.0.0.1:18000/health
curl -fsS http://127.0.0.1:18000/v1/models
curl -fsS http://127.0.0.1:18000/metrics >/dev/null && echo metrics-ok
```

The gate is:

- a `LISTEN` entry for `127.0.0.1:18000`;
- `health_http=200`;
- the expected Qwen model in `/v1/models`;
- `metrics-ok`.

The `/health` response may have an empty body. Use the explicit HTTP status
check rather than treating an empty body as failure.

## Bind the assignment runtime to the tunnel

The direct-H100 template defaults to `http://127.0.0.1:8000/v1`. The CPU
controller must use the tunneled address
`http://127.0.0.1:18000/v1`. Render a new external runtime manifest for each
reviewed checkout; do not overwrite a historical manifest.

From the CPU repository checkout, render and then update only the transport
endpoint:

```bash
repo_root=/mnt/eic-work/repos/agentic-workload-simulator
work_root=/mnt/eic-work
runtime_manifest=/mnt/eic-work/assignment/runtime-manifest-pace-tunnel.json

python3 scripts/assignment/render_runtime_manifest.py \
  --repo-root "$repo_root" \
  --work-root "$work_root" \
  --hardware h100 \
  --expected-branch parallel-h100-shards \
  --output "$runtime_manifest"

tmp_manifest="${runtime_manifest}.tmp"
jq '.model.api_base = "http://127.0.0.1:18000/v1"' \
  "$runtime_manifest" > "$tmp_manifest"
mv -- "$tmp_manifest" "$runtime_manifest"

manifest_dir="$(dirname -- "$runtime_manifest")"
manifest_name="$(basename -- "$runtime_manifest")"
(
  cd "$manifest_dir" &&
  sha256sum "$manifest_name" > "${manifest_name}.sha256"
)
chmod 600 "$runtime_manifest" "${runtime_manifest}.sha256"

jq -e '.model.api_base == "http://127.0.0.1:18000/v1"' \
  "$runtime_manifest"
```

The sidecar must contain the manifest basename, not an absolute path. The
runtime manifest and sidecar are part of the run identity; record their hashes
with the plan and raw artifacts.

Only after the endpoint gate, manifest validation, plan validation, and the
assignment's explicit execution authorization should the matrix runner be
started. Pass this manifest to the reviewed runner; do not substitute
`127.0.0.1:8000` on the CPU VM.

Validate the sealed plan and runtime without starting a case:

```bash
repo_root=/mnt/eic-work/repos/agentic-workload-simulator
runtime_manifest=/mnt/eic-work/assignment/runtime-manifest-pace-tunnel.json
plan_path=/mnt/eic-work/assignment/plan.jsonl
plan_sidecar=/mnt/eic-work/assignment/plan.jsonl.sha256
matrix_output=/mnt/eic-work/assignment/matrix-runs

python3 "$repo_root/scripts/assignment/run_matrix.py" \
  --plan "$plan_path" \
  --sha256-sidecar "$plan_sidecar" \
  --runner "$repo_root/scripts/assignment/sweagent_case_runner.py" \
  --runtime-manifest "$runtime_manifest" \
  --output-dir "$matrix_output"
```

Only after the separate execution gate, deadline, and artifact-root checks
pass, add `--execute --acknowledge-paid-gpu-work --resume` to that same
command. The runner enforces concurrency 1 and persists resumable state; keep
the CPU-side tunnel alive for the entire execution.

## Reusing the path for multiple runs

Keep these long-lived components unchanged while the serialized runs proceed:

1. the PACE vLLM server on compute-node port 8000;
2. the PACE compute-to-login `-R` relay;
3. the CPU VM OpenConnect process and `tun0` interface;
4. the CPU VM login-to-local `-L` tunnel.

Before each run:

```bash
curl -sS -o /dev/null -w 'health_http=%{http_code}\n' \
  http://127.0.0.1:18000/health
curl -fsS http://127.0.0.1:18000/v1/models >/dev/null
curl -fsS http://127.0.0.1:18000/metrics >/dev/null
```

For each run, use a unique `run_id`, output directory, and attempt identity.
Capture vLLM aggregate Prometheus snapshots at the run boundaries without
assigning them to an individual SWE-agent request:

```bash
python3 scripts/observability/scrape_vllm.py \
  --url http://127.0.0.1:18000/metrics \
  --output /mnt/eic-work/assignment/raw/RUN_ID/vllm_metrics_start.json \
  --raw-output /mnt/eic-work/assignment/raw/RUN_ID/vllm_metrics_start.prom \
  --run-id RUN_ID \
  --snapshot-kind start
```

Repeat with `--snapshot-kind end` after the run. Request-level model timing
must continue to come from the reviewed request proxy/runner boundaries and
their model-event artifacts; aggregate vLLM counters are server-level
diagnostics only.

Do not clear or merge another run's raw directory. If a run is interrupted,
preserve its `run_state.json`, unavailable reason, partial trajectory, logs,
and hashes, then resume according to the assignment runbook.

## Teardown

After all runs and exports are complete:

1. stop the assignment runner and flush its artifacts;
2. stop the CPU-side `ssh -fNT` process by its exact PID after checking
   `ps -eo pid,args | rg 'ssh .*18000'`;
3. stop OpenConnect with `Ctrl-C` in its own terminal and wait for the
   successful logout/removal of `tun0`;
4. stop the PACE reverse relay by its exact job/process identity;
5. stop vLLM using the PACE job/service manager;
6. hash and export artifacts before releasing any temporary storage.

Do not use broad `pkill` patterns while a valid VPN, relay, or vLLM process is
running; multiple runs can have similarly named processes.

## Troubleshooting

### `gcloud compute ssh` says insufficient authentication scopes

The command was run inside the VM. Run it from Cloud Shell, whose prompt ends
in `@cloudshell`, not from a prompt ending in `@instance-...`.

### The shell reports `command not found` for `jasonrivera...$`

The prompt or previous output was pasted as part of the command. Press
`Ctrl-C` once if the shell shows a continuation prompt (`>`), then paste only
the command inside the code block.

### OpenConnect appears to be doing nothing

A foreground VPN process intentionally owns its terminal. Password and Duo
input may not echo. `ESP tunnel connected` means it succeeded; do not interrupt
it. If authentication loops with HTTP 512 before a tunnel is established,
inspect for stale OpenConnect processes, stop only the failed exact process,
and retry after confirming the GCP route exceptions. Do not run duplicate
authentication sessions against the same VM.

### The IAP SSH session freezes after VPN startup

The VPN changed the default route used by the IAP management connection. Stop
only the failed VPN attempt if it has not reached `ESP tunnel connected`, then
restore the explicit `35.235.240.0/20` and metadata routes before retrying.
Do not use the serial console to guess a Linux password; GCP SSH users often do
not have a local console password.

### `nc` to the PACE relay times out

Confirm `tun0` exists, check `ip route get 128.61.254.151`, and verify that the
PACE relay process is still connected. Try the prepared fallback relay
`128.61.254.154` only if its PACE-side reverse relay is also running.

### The CPU VM has no `18000` listener

The local `-L` command did not remain authenticated, the port is already
occupied, or `ExitOnForwardFailure` rejected the bind. Check:

```bash
ss -ltnp | grep ':18000' || echo 'no local tunnel listener'
ps -eo pid,args | rg 'ssh .*18000' || true
```

Run the interactive `pace-ssh-ok` test first, then recreate the forward
without `-i ~/.ssh/id_rsa` unless that file actually exists.

### The model endpoint works on PACE but not on the CPU VM

Check the two legs independently: PACE compute `127.0.0.1:8000`, then PACE
login relay `127.0.0.1:18000`, then CPU `127.0.0.1:18000`. Do not point the
CPU tunnel at an ephemeral compute-node hostname and do not expose port 8000
publicly.

## Evidence to record for every session

Keep a small session record outside the Git checkout containing:

- UTC start/end times and the PACE allocation/job ID;
- PACE compute hostname and relay hostname/IP selected;
- CPU VM name, zone, and repository commit;
- OpenConnect and SSH command fingerprints, excluding passwords and tokens;
- vLLM model ID, model revision, vLLM version, and server manifest hash;
- CPU-side runtime manifest and sidecar hashes;
- plan and plan-sidecar hashes;
- endpoint gate results (`health`, model ID, metrics);
- run IDs, raw artifact roots, unavailable rows, and final inventory hashes.

This record makes repeated runs auditable without treating a healthy tunnel as
evidence that a workload completed or that a row was resolved.
