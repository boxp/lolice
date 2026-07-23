# BOXP-23 local-llm q8 V cache

## Context

Gemma4 returns corrupted Japanese output in production when `local-llm` runs with `--cache-type-v turbo3`.

The issue was reproduced with direct `local-llm` API calls, so it is not isolated to pi agent, output length, or context window settings.

Host-side comparison on `golyat-4` showed:

- official upstream `llama-server` with `--cache-type-v q8_0` returned normal Japanese for `こんにちは`
- TurboQuant `llama-server` with `--cache-type-v q8_0` returned normal Japanese for the same prompt
- production `local-llm` with `--cache-type-v turbo3` returned corrupted Japanese for the same prompt
- TurboQuant host binary with `--cache-type-v turbo3` aborted during load with `SET_ROWS` on `cache_v_l0 (view)`

## Plan

1. Change the production `local-llm` Deployment V cache from `turbo3` to `q8_0`.
2. Keep the TurboQuant `llama-sycl` image, `-ngl 99`, `-fa on`, `--jinja`, `--reasoning off`, `--cpu-moe`, router mode, and `--models-max 1`.
3. Do not edit the workload image tag or digest. `argocd-image-updater` owns image updates.

## Validation

- `kubectl kustomize argoproj/local-llm`
- server-side dry-run of rendered `argoproj/local-llm`
- after merge and Argo sync:
  - `local-llm` reaches `Synced Healthy`
  - `/v1/models` shows the configured models
  - direct API smoke for `gemma4-26b` with `こんにちは` returns readable Japanese
  - direct API smoke for the controlled Japanese prompt returns `了解しました。`

## Follow-up

Track `turbo3` as a separate correctness/performance target. It should not block production Japanese correctness while the current path reproduces corrupted output and `SET_ROWS` failures.
