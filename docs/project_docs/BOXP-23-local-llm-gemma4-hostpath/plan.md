# BOXP-23 local LLM Gemma4 hostPath rollout

## Context

`local-llm` TinyLlama smoke passed with `ghcr.io/boxp/arch/llama-sycl`.

Production model weights are too large for Longhorn replica storage. Gemma4 Q4_K_M is about 16.8GB and Ornith Q4_K_M is about 21.2GB, excluding partial downloads and replacement files.

## Plan

1. Remove the smoke-only Longhorn PVC and TinyLlama initContainer from `local-llm`.
2. Mount `golyat-4` local model storage from `/var/lib/local-llm/models` with `hostPath`.
3. Start with Gemma4 as a single model before enabling router mode.
4. Keep the workload pinned to the GPU worker and requesting `gpu.intel.com/i915: 1`.
5. Verify `/health`, `/v1/models`, `/v1/chat/completions`, and SYCL runtime logs after Argo CD sync.

## Validation

- `kubectl kustomize argoproj/local-llm`
- `kubectl kustomize argoproj`
- `git diff --check`
- server-side dry-run if the cluster API is reachable
- runtime smoke from inside the cluster
