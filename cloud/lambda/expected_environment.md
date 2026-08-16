# Expected Lambda environment

This is a preflight checklist, not a claim about the current VM. The actual
console image, SKU, driver, CUDA, and price must be recorded at launch.

Expected target:

- one isolated H100-class GPU;
- at least 80 GB usable VRAM;
- 26 vCPUs and approximately 225 GiB RAM for the listed 1x H100 PCIe SKU;
- Ubuntu/Linux x86-64;
- Lambda Stack or another reviewed image with a compatible NVIDIA driver;
- Docker available for the official SWE-bench evaluator;
- enough local/persistent storage for the model, selected evaluator images,
  repositories, logs, and result export.
- vLLM is bound to localhost and uses one explicitly leased CUDA device. The
  launcher records hostname, PID, task/experiment IDs, GPU, port, config hash,
  and UTC acquisition time in the lease metadata; a stale-looking lease is
  not cleared while a port or CUDA process is active.

The preflight script must validate capability rather than requiring one exact
`nvidia-smi` marketing string. Any materially different SKU, precision,
parallelism, or billing configuration requires a root decision record.

References checked 2026-08-15:

- https://lambda.ai/instances
- https://docs.lambda.ai/public-cloud/console/
- https://docs.lambda.ai/public-cloud/access-security/
