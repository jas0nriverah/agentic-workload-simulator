# Portable direct H100 runtime

The direct runtime is intentionally separate from final-assignment validation.
It does not start SWE-agent, Docker, the evaluator, profiling, or a workload.

`cloud/gcp/h100_direct_runtime_requirements.txt` declares the compatible
top-level inputs:

```text
vllm==0.10.0
transformers>=4.57.6,<5
tokenizers>=0.22.2,<0.23
huggingface-hub>=0.34.4,<1
```

Bootstrap or reuse an external environment and let pip or uv resolve its
transitive dependencies normally:

```bash
scripts/cloud/start_h100.sh --manifest /path/to/h100-startup.env \
  --backend direct --setup
```

The setup is idempotent: an existing Python 3.11 environment is reused, the
model is reused only when its exact revision and file metadata verify, and
runtime checks require vLLM 0.10.0, Torch 2.7.1, Transformers 4.57.6 or
later within the declared pre-5 range, compatible Tokenizers and
huggingface-hub versions within the declared ranges, CUDA, one H100 with at
least 80,000 MiB, and `all_special_tokens_extended` on the local tokenizer.

After setup, the normal direct launch uses the same manifest-pinned model,
revision, BF16, TP=1, 32,768 context, 0.90 GPU utilization, localhost port
8000, automatic tool choice, and `qwen3_coder` parser. It performs health,
model, metrics, warmup, GPU, and log checks before reporting ready. Keep the
manifest, environment, model cache, logs, and runtime state outside the
checkout; never commit them.

Final assignment validation remains a separate, explicitly authorized flow.
