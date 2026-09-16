# Worker runtime inputs: 2026-09-09

`scripts/assignment/render_worker_runtime_inputs.py` creates offline, hashed
hardware profiles and runtime templates for the 22 serving workers selected by
the root allocation-v4 fingerprint. It reads JSON only. It does not probe an
API, start a GPU job, change a relay, or write a final runtime manifest.

The concrete staged output is:

```text
/tmp/assignment-worker-runtime-inputs-20260909-lunamax-v3
```

The output index is
`worker_runtime_inputs_manifest.json` with SHA-256
`83d59cf524877719a252d11512e06dc0c806dafdb94ca7658fd9c5d00026f11a`.
`artifact_hashes.json` has SHA-256
`8449b595b17e1f5899a1533bf9f81da17e741fff9033f3888f49cafc47ae161f`.

## Inputs and scope

The root source is:

```text
/home/riverahernandezjason/h100-assignment-work-20260905/assignment/submission/20260909T000000Z-resume/verification/worker-fingerprint-allocation-v4/fingerprints.json
```

Its source-file SHA-256 is
`2c9c45d9ab5f15b5e7a9eda19a4a782934a43018fcd2677a98d01fe0908aa34a`.
It reports the read-only `existing_allocation_overlap_step` scope and was
captured at `2026-09-09T01:12:27.552884+00:00`.

The renderer also records the checked-in example
`configs/assignment_runtime_manifest.example.json` (SHA-256
`af82494fae9388cba93507c4656fd248757f5deb6e1c10976c405d301b68256e`) and
the separate local CPU/VM inventory manifest. The CPU/VM binding artifact has
SHA-256 `83af52857ee02e4354f71eda05bf9c48def66cabe4199303607efcb29a5b8b20`.
It retains that manifest as a separate reference and does not merge CPU data
into a worker GPU profile.

The command used was:

```bash
python3 scripts/assignment/render_worker_runtime_inputs.py \
  --fingerprints /home/riverahernandezjason/h100-assignment-work-20260905/assignment/submission/20260909T000000Z-resume/verification/worker-fingerprint-allocation-v4/fingerprints.json \
  --runtime-template configs/assignment_runtime_manifest.example.json \
  --cpu-vm-profile /home/riverahernandezjason/h100-assignment-work-20260905/assignment/submission/20260909T000000Z-resume/verification/local-cpu-inventory/manifest.json \
  --output-dir /tmp/assignment-worker-runtime-inputs-20260909-lunamax-v3
```

## Produced bindings

The selected worker IDs are exactly `00` through `10` and `12` through `22`.
Each has one exact observed GPU UUID and one serving process. Hardware files
are under `hardware-profiles/worker-XX.json`; templates are under
`runtime-templates/worker-XX.json`.

| Worker | Job | Node | API PID | Start ticks | GPU UUID | Local | Remote | Observed alias |
| --- | --- | --- | ---: | ---: | --- | ---: | ---: | --- |
| 00 | 5735426 | atl1-1-03-011-3-0 | 2597014 | 233243377 | GPU-90102672-9e47-7efc-979b-b0cb420d3ccf | 18100 | 18200 | Qwen/Qwen3-Coder-30B-A3B-Instruct |
| 01 | 5735436 | atl1-1-03-011-13-0 | 2670696 | 225412853 | GPU-e633211c-d9b0-8dad-c241-eb1ce5176cb9 | 18101 | 18201 | Qwen/Qwen3-Coder-30B-A3B-Instruct |
| 02 | 5735437 | atl1-1-03-010-25-0 | 958230 | 234370683 | GPU-750805c5-4e2c-1be5-d35f-b8eabd94fcea | 18102 | 18202 | Qwen3-Coder-30B-A3B-Instruct |
| 03 | 5735435 | atl1-1-03-010-20-0 | 1721823 | 233439308 | GPU-6e9ea26b-82cc-12a3-fe14-241ec5dd2855 | 18103 | 18203 | Qwen3-Coder-30B-A3B-Instruct |
| 04 | 5735433 | atl1-1-03-010-10-0 | 2438496 | 234375635 | GPU-4d49f39c-2c56-66a9-eff0-d717e4ff9cc1 | 18104 | 18204 | Qwen3-Coder-30B-A3B-Instruct |
| 05 | 5735430 | atl1-1-03-010-30-0 | 3889758 | 233489928 | GPU-95d5c49d-011c-bd20-e408-324e9d7f2628 | 18105 | 18205 | Qwen3-Coder-30B-A3B-Instruct |
| 06 | 5735429 | atl1-1-03-011-8-0 | 24411 | 410449 | GPU-501a8d1b-1534-0ffb-d891-0f3d0e6e04df | 18106 | 18206 | Qwen/Qwen3-Coder-30B-A3B-Instruct |
| 07 | 5735428 | atl1-1-03-011-8-0 | 24419 | 410526 | GPU-c4b285e2-a342-2942-49f4-bd8d51d9f78a | 18107 | 18207 | Qwen/Qwen3-Coder-30B-A3B-Instruct |
| 08 | 5735427 | atl1-1-03-011-8-0 | 24454 | 410605 | GPU-84d78cf4-a821-e7c1-2fcb-503332915ad2 | 18108 | 18208 | Qwen/Qwen3-Coder-30B-A3B-Instruct |
| 09 | 5737067 | atl1-1-03-011-23-0 | 1549570 | 234289590 | GPU-74573fbf-4115-8134-0e39-eb8b260d5e30 | 18109 | 18209 | Qwen3-Coder-30B-A3B-Instruct |
| 10 | 5736459 | atl1-1-03-010-30-0 | 3935595 | 234267404 | GPU-61961aed-2b41-f72c-2ec1-f53e7bb03e03 | 18110 | 18210 | Qwen3-Coder-30B-A3B-Instruct |
| 12 | 5737068 | atl1-1-03-011-23-0 | 1549637 | 234290556 | GPU-ea5cd554-83cb-7285-ded3-f56b93f14f9c | 18112 | 18212 | Qwen3-Coder-30B-A3B-Instruct |
| 13 | 5729626 | atl1-1-03-011-8-0 | 84948 | 1522194 | GPU-f741acc1-09c9-4130-f4dd-42b96382479b | 18113 | 18213 | Qwen3-Coder-30B-A3B-Instruct |
| 14 | 5729625 | atl1-1-03-011-18-0 | 3125785 | 223910850 | GPU-233bf518-b203-acc6-04ec-11dfb2e2f856 | 18114 | 18214 | Qwen3-Coder-30B-A3B-Instruct |
| 15 | 5729624 | atl1-1-03-011-18-0 | 3126282 | 223913376 | GPU-cc25da96-512a-d374-5ecf-60c4ee4ec630 | 18115 | 18215 | Qwen3-Coder-30B-A3B-Instruct |
| 16 | 5735422 | atl1-1-03-010-20-0 | 1709784 | 233248067 | GPU-61a9e5d5-cd9d-4888-f96e-587ac242c2f0 | 18116 | 18216 | Qwen/Qwen3-Coder-30B-A3B-Instruct |
| 17 | 5736458 | atl1-1-03-010-20-0 | 1797938 | 234309702 | GPU-d1ab0d90-760b-eb71-c40b-1c56bffc9c2a | 18117 | 18217 | Qwen3-Coder-30B-A3B-Instruct |
| 18 | 5735423 | atl1-1-03-010-25-0 | 772390 | 233262219 | GPU-2ece97c7-9e39-6bac-3eda-9546ec5507a4 | 18118 | 18218 | Qwen/Qwen3-Coder-30B-A3B-Instruct |
| 19 | 5735425 | atl1-1-03-010-30-0 | 3879654 | 233259301 | GPU-345d2513-7766-b4f5-c5a6-d35ff2510a43 | 18119 | 18219 | Qwen/Qwen3-Coder-30B-A3B-Instruct |
| 20 | 5741126 | atl1-1-03-012-3-0 | 3564437 | 113973620 | GPU-1170eb44-647a-cd2b-f3af-3d94b5d4d308 | 18120 | 18220 | Qwen/Qwen3-Coder-30B-A3B-Instruct |
| 21 | 5741125 | atl1-1-03-012-3-0 | 3564436 | 113973620 | GPU-7f6bef3f-a470-50c1-9891-e2129ad5a75c | 18121 | 18221 | Qwen/Qwen3-Coder-30B-A3B-Instruct |
| 22 | 5741123 | atl1-1-03-011-28-0 | 3155525 | 236427612 | GPU-f62c1d77-1a4a-36db-605e-74d694f45aa5 | 18122 | 18222 | Qwen/Qwen3-Coder-30B-A3B-Instruct |

The source contained two allocation records without a serving process, so
they were retained as excluded records rather than silently converted into
workers:

| Job | Node | GPU UUID | Reason |
| --- | --- | --- | --- |
| 5736512 | atl1-1-03-011-13-0 | GPU-b9b31a1c-0055-014b-4ccc-300a9f3dfc4e | no serving process observed |
| 5736461 | atl1-1-03-010-30-0 | GPU-0b995e5f-65d0-6256-4d6f-9de0e11a805a | no serving process observed |

## Readiness boundary

Every worker carries the exact observed serving options, raw `nvidia-smi`
output, driver version, VRAM total, PCI identity, observed SM/memory clocks,
power limit, temperature, model path, and served-model alias. The observed
`--max-model-len` is `32768` for all 22 workers. The target gate remains
`65536`; no readiness or endpoint success is inferred from this fingerprint.

The templates retain placeholders for the source 65K binding, observer 65K
binding, remote fixed model revision, and clean source commit/integrity render.
Each template has `status: template_only`, `launchable: false`, and the same
four pending reason codes. No GPU bandwidth or CPU frequency is fabricated;
those measurements remain explicitly unknown.

The concrete first-case handoff after all gates resolve is recorded in the
index as `first_case_inputs_after_gates`: worker `00`,
`runtime-templates/worker-00.json`,
`hardware-profiles/worker-00.json`, and `cpu_vm_profile_binding.json`. These
are staged inputs, not permission to launch them.

## Validation

The focused test file
`tests/assignment/test_render_worker_runtime_inputs.py` passes 2 tests. It
covers exact worker/port/alias binding, raw observed values, separate CPU/VM
handling, non-launchable pending gates, and duplicate UUID rejection. The
renderer’s real-source run produced 22 profiles and 22 templates, with a
sidecar hash for every JSON artifact.

The completed v3 directory was copied without overwrite into this new durable
verification directory after reconnect:

```text
/home/riverahernandezjason/h100-assignment-work-20260905/assignment/submission/20260909T000000Z-resume/verification/worker-runtime-inputs-v3-handoff-20260909-txnh0lc5/templates-v3
```

All 94 files match the original byte-for-byte, including all JSON hash
sidecars; the index hash above is unchanged. The parent directory retains
`archive_manifest.json`, focused test logs (2 passed), and source snapshots at
handoff. The archive manifest SHA-256 is
`dc8fe2916aaf1b6c41be006a10eaad0b47a50e6856b73287f2e4d9b4bcae73d2`.
The original `/tmp` directory remains intact. This closes template generation
and archival only; the live evaluator integration handoff is in
`docs/EVALUATOR_READONLY_CHECK_20260909.md`.
