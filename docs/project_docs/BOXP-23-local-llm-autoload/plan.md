# BOXP-23 local-llm autoload

## Goal

Start the local LLM router with all production models unloaded, then load the requested model on demand from the request body.

## Context

The previous production router used `--models-max 1`, `--no-models-autoload`, and `load-on-startup = true` for `gemma4-26b`. That made Gemma4 load at startup and made `ornith-35b` return `400 model is not loaded` unless `/models/load` was called manually.

The desired behavior is:

- `llama-server` starts with `gemma4-26b` and `ornith-35b` both unloaded.
- A request with `model: gemma4-26b` loads Gemma4 on demand.
- A request with `model: ornith-35b` loads Ornith on demand.
- `--models-max 1` remains in place so only one model is resident at a time.

## Changes

- Remove `--no-models-autoload` from the `local-llm` Deployment.
- Remove `load-on-startup = true` from the Gemma4 router preset.
- Keep Ooedo/Lunar runtime sizing and Intel SYCL settings unchanged.

## Validation

- `kubectl kustomize argoproj/local-llm`
- `git diff --check`
- Server-side dry-run against the live cluster.
- After merge: verify `/v1/models` starts with both production models unloaded, then pi/API requests autoload Gemma4 and Ornith successfully.
