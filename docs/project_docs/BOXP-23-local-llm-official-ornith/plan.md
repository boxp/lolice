# BOXP-23 local-llm official Ornith

## Context

Phase 6 needs the `local-llm` router to expose both `gemma4-26b` and `ornith-35b` with `--models-max 1`.

The initially seeded `zaakirio/Ornith-1.0-35B-uncensored-GGUF` Q4_K_M file loaded but produced malformed output on both Ooedo and lolice. The official `deepreinforce-ai/Ornith-1.0-35B-GGUF` Q4_K_M file was then seeded to `golyat-4:/var/lib/local-llm/models/ornith-1.0-35b-Q4_K_M.gguf` and passed standalone and router smoke tests.

## Plan

- Add `ornith-35b` to `argoproj/local-llm/models-config.yaml`.
- Keep `gemma4-26b` as the startup-loaded model.
- Point `ornith-35b` at the official GGUF file: `/models/ornith-1.0-35b-Q4_K_M.gguf`.
- Include Ornith-specific conservative settings from the verified smoke: `ctx-size 65536`, `batch-size 128`, `ubatch-size 32`, `reasoning-budget 0`.
- Do not edit the `local-llm` workload image tag or digest. Image state is managed by `argocd-image-updater`.

## Verification

- `kubectl kustomize argoproj/local-llm`
- Temporary in-pod router smoke on `golyat-4`:
  - initial `gemma4-26b` loaded and returned `OK`
  - `POST /models/load {"model":"ornith-35b"}` unloaded Gemma4 and loaded Ornith
  - `ornith-35b` returned `OK`
  - `POST /models/load {"model":"gemma4-26b"}` unloaded Ornith and reloaded Gemma4
  - `gemma4-26b` returned `OK`
- After merge/sync:
  - Argo CD `local-llm` reaches `Synced Healthy`
  - `/v1/models` shows `gemma4-26b` and `ornith-35b`
  - `gemma4-26b` and `ornith-35b` each pass a short `Return exactly: OK` completion through the Service
