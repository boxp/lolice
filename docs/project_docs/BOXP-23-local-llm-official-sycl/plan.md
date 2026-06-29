# BOXP-23 local-llm official SYCL rollout

## Context

`golyat-4` host direct verification showed that official `ggml-org/llama.cpp` commit `8c146a8366304c871efc26057cc90370ccf58dad` works with oneAPI 2026.0, Level Zero, and `SYCL0` on Intel Arc Graphics.

`boxp/arch#10646` rebuilt `ghcr.io/boxp/arch/llama-sycl:latest` from that official source and published:

- index digest: `sha256:5c781ee682d4c8afa21799ad36a9229895663e32479f57aac3736cd2d8aac36a`
- linux/amd64 manifest: `sha256:5ec040afa0ce9ca046d3a0217b7217f1082dc6e9d54244c47acaf47dc2f801d6`

## Plan

1. Switch `local-llm` image back from `ghcr.io/boxp/arch/llama-vulkan:latest` to `ghcr.io/boxp/arch/llama-sycl:latest`.
2. Pin the current Argo CD kustomize image state to the newly published `llama-sycl` digest.
3. Switch runtime device from `Vulkan0` to `SYCL0`.
4. Keep the current production model settings: `-ngl 99`, `-c 65536`, `--cache-type-k q8_0`, `--cache-type-v q8_0`, `--cpu-moe`, `--flash-attn on`, `--models-max 1`.
5. Keep image digest ownership under `argocd-image-updater`, but change its target back to `llama-sycl`.
6. Roll out through Argo CD and verify logs show `SYCL0` model/KV/compute buffers, readable Japanese output, and token/s.

## Verification

- `kubectl kustomize argoproj/local-llm`
- `kubectl kustomize argoproj/argocd-image-updater`
- server-side dry-run for both rendered manifests
- production rollout smoke after merge
