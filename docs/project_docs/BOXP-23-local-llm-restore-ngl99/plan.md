# BOXP-23 local-llm restore NGL99

## Context

`local-llm` was pinned to `-ngl 3` after Gemma4 / Intel SYCL output corruption appeared at higher GPU offload levels. Current runtime investigation shows GPU compute is active, but token throughput is lower than host-side expectations and the root cause may be outside `-ngl` itself.

## Plan

1. Restore the `local-llm` Deployment `-ngl` value to `99` for high GPU offload validation.
2. Keep `--models-max 1`, `--cache-type-v turbo3`, `-fa on`, `--cpu-moe`, and the current model router settings unchanged.
3. Do not edit the workload image tag or digest; image state remains managed by `argocd-image-updater`.
4. Validate rendered manifests and server-side dry-run before merge.
5. After Argo CD rollout, verify actual llama-server args, GPU utilization, token throughput, and output correctness separately.

## Validation

- `kubectl kustomize argoproj/local-llm`
- Server-side dry-run of rendered `argoproj/local-llm`
- Live rollout reaches `Synced Healthy`
- Runtime logs show spawned model args include `--n-gpu-layers 99`
