# BOXP-23 pi agent Ornith registration

## Goal

Register the production `ornith-35b` router alias in the GitOps-managed `codex-workspace` pi agent config and align the advertised pi context window with the `local-llm` router runtime.

## Context

`local-llm` already exposes both aliases through the same OpenAI-compatible endpoint:

- `gemma4-26b`
- `ornith-35b`

The existing `codex-workspace-pi-config` only listed `gemma4-26b`, so pi agent could not select Ornith.

The existing pi config advertised `contextWindow=4096`, and the `local-llm` Deployment also passed `-c 4096`, so long pi prompts were genuinely truncated at the router. Phase 6 target usage needs about 62K context, so this change uses `65536`.

Gemma4 Japanese output quality is being treated as a separate runtime/model issue. The direct `local-llm` API also returned malformed Japanese for a short Japanese prompt, so this is not isolated to pi agent or only an output limit problem.

## Changes

- Add `ornith-35b` to `argoproj/codex-workspace/configmap.yaml`.
- Set `contextWindow=65536` for both local pi models.
- Set `local-llm` router context to `65536` so the advertised pi context matches the actual server runtime.
- Restore Ooedo/Lunar batch sizing for router presets:
  - `gemma4-26b`: `ctx-size 65536`, `batch-size 256`, `ubatch-size 64`
  - `ornith-35b`: `ctx-size 65536`, `batch-size 128`, `ubatch-size 32`
- Keep the same `llama.cpp` provider and endpoint:
  - `http://llama-server.local-llm.svc.cluster.local:8080/v1`
- Keep `reasoning=false` because the provider is configured with `supportsReasoningEffort=false`.

## Validation

- Parse the embedded `models.json`.
- Render `argoproj/codex-workspace` with `kubectl kustomize`.
- Render `argoproj/local-llm` with `kubectl kustomize`.
- Server dry-run both `argoproj/codex-workspace` and `argoproj/local-llm`.
- Confirm `local-llm` keeps `--models-max 1`, so only one of Gemma4 / Ornith is loaded at a time.
- After deployment, verify:
  - `pi --list-models llama` shows `ornith-35b`.
  - `/v1/models` shows `gemma4-26b` and `ornith-35b` with `n_ctx=65536` after each model is loaded.
  - `pi --provider llama.cpp --model ornith-35b --print --no-tools --no-session --thinking off --no-context-files 'Return exactly: OK'` returns `OK`.

## Local validation

- Parsed embedded `models.json`; models are `gemma4-26b` and `ornith-35b`, both with `contextWindow=65536`.
- `kubectl kustomize argoproj/codex-workspace`
- `kubectl kustomize argoproj/local-llm`
- `kubectl --server=https://192.168.10.102:6443 apply --dry-run=server -k argoproj/codex-workspace`
- `kubectl --server=https://192.168.10.102:6443 apply --dry-run=server -k argoproj/local-llm`
