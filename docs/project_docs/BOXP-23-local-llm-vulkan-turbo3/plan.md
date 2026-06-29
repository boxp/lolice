# BOXP-23 local-llm Vulkan turbo3 production switch

## Context

`golyat-4` host smoke showed that TheTom `llama-cpp-turboquant` commit `a33ef00b1` with Vulkan can start Gemma4 using `--cache-type-v turbo3` and return readable Japanese. The current production SYCL path has shown unstable behavior around `q8_0` and the cclecle `turbo3` path.

The image itself is still updated by `argocd-image-updater`; this change updates the `local-llm` image name, ImageUpdater target, runtime args, and resource policy in `boxp/lolice`.

## Plan

1. Switch `local-llm` image from `ghcr.io/boxp/arch/llama-sycl:latest` to `ghcr.io/boxp/arch/llama-vulkan:latest`.
2. Update the `local-llm` ImageUpdater target to track `llama-vulkan` by digest.
3. Switch runtime device from `-dev SYCL0` to `-dev Vulkan0`.
4. Restore `--cache-type-v turbo3` for the production model server.
5. Remove CPU and memory hard limits from the `llama-server` container while keeping requests and the Intel GPU extended resource limit. This avoids cgroup memory pressure competing with the iGPU's shared-memory allocation path.
6. Keep router mode, `--models-max 1`, `-ngl 99`, `-c 65536`, Flash Attention, Jinja, reasoning off, CPU MoE, and existing model presets.
7. Let ImageUpdater manage the image digest after the matching `boxp/arch` image PR publishes the TheTom+Vulkan image.

## Validation

- `kubectl kustomize argoproj/local-llm`
- `kubectl kustomize argoproj/argocd-image-updater`
- server-side dry-run for `argoproj/local-llm`
- After image rollout: `/health`, `/v1/models`, Gemma4 Japanese smoke, model switch smoke, and token/s / GPU utilization check.
