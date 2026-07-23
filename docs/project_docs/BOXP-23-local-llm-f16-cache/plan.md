# BOXP-23 local-llm f16 V cache

## Context

`boxp/lolice#646` changed production `local-llm` from `--cache-type-v turbo3` to `q8_0` to stop Gemma4 Japanese output corruption.

That restored output correctness, but live production smoke showed an unacceptable first prompt eval slowdown:

- `こんにちは` returned readable Japanese
- controlled Japanese prompt returned `了解しました。`
- first prompt eval was about `0.079 tokens/s`
- `intel_gpu_top` showed GPU Compute `0%`

A one-off test inside the live `local-llm` Pod used the same image, model, GPU device, `-ngl 99`, Flash Attention, Jinja, reasoning off, and CPU MoE, but changed only `--cache-type-v f16`.

Results:

- `こんにちは` returned readable Japanese
- a longer Japanese response also returned readable Japanese
- first prompt eval was `11.52 tokens/s` for the short prompt
- first prompt eval was `20.20 tokens/s` for the longer prompt
- generation was about `6.80` to `7.82 tokens/s`
- no `SET_ROWS` abort occurred

## Plan

1. Change production `local-llm` V cache from `q8_0` to `f16`.
2. Keep `--cache-type-k q8_0`, the TurboQuant image, `-ngl 99`, `-fa on`, `--jinja`, `--reasoning off`, `--cpu-moe`, router mode, and `--models-max 1`.
3. Do not edit the workload image tag or digest. `argocd-image-updater` owns image updates.

## Validation

- `git diff --check`
- `kubectl kustomize argoproj/local-llm`
- server-side dry-run of rendered `argoproj/local-llm`
- `codex review --uncommitted`
- after merge and Argo sync:
  - `local-llm` reaches `Synced Healthy`
  - live Deployment args include `--cache-type-v f16`
  - direct API smoke for `gemma4-26b` with `こんにちは` returns readable Japanese
  - direct API smoke for the controlled Japanese prompt returns `了解しました。`

## Follow-up

Continue tracking `turbo3` as a separate correctness/performance target. The current production path should favor readable output and non-pathological first-token performance.
