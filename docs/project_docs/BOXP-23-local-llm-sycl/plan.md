# BOXP-23 local LLM SYCL workload plan

## Context

`golyat-4` is the dedicated Intel GPU worker. It is labeled `lolice.io/gpu-worker=true`, tainted `lolice.io/gpu-worker=true:NoSchedule`, and exposes `gpu.intel.com/i915=1` through the Intel GPU device plugin.

## Plan

1. Consume `ghcr.io/boxp/arch/llama-sycl`, built from `TheTom/llama-cpp-turboquant` `feature/turboquant-kv-cache`.
2. Add a `local-llm` Argo CD Application.
3. Add a `llama-server` Deployment that:
   - requests `gpu.intel.com/i915: 1`
   - selects `lolice.io/gpu-worker=true`
   - tolerates `lolice.io/gpu-worker=true:NoSchedule`
   - exposes an internal ClusterIP service
4. Use a small TinyLlama GGUF and a 5Gi Longhorn PVC for the first Kubernetes smoke.
5. After the GHCR image exists, verify `/health`, `/v1/models`, `/v1/chat/completions`, and GPU offload logs.

## Follow-up

- Replace the smoke model setup with the selected production model storage layout.
- Add router preset for `gemma4-26b` first.
- Add `ornith-35b` after router/output behavior is verified.
