# Decisions

## D-0001 - Minimal G0 bootstrap

- Status: accepted
- Decision: start with dependency-light JSON configuration and a lossless event envelope; resolve Linux/CUDA dependencies before selecting the final runtime stack.
- Rationale: local host is macOS arm64 and does not prove Lambda Linux/CUDA compatibility.
- Assignment impact: none; no final empirical result exists.

## D-0002 - Lambda target

- Status: proposed
- Decision: prepare for one Lambda H100 PCIe 80 GB instance.
- Rationale: enough VRAM for the preferred Qwen configuration candidate, with final fit tested on the actual host.
- Approval needed: user billing authorization before launch.
