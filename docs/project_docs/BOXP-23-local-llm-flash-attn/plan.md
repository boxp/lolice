# Plan: local-llm Flash Attention

## Context

`local-llm` runs Gemma4 on `golyat-4` with `--cache-type-v turbo3`. After moving the image to the `cclecle` SYCL `SET_ROWS` source ref, the original abort changed to a Flash Attention failure:

- `quantized V cache was requested, but this requires Flash Attention`
- `Flash Attention was auto, set to disabled`
- process exited with code 139

A one-off pod using the same GPU worker security context started successfully with `--cache-type-v turbo3 -fa on`; `/health`, `/v1/models`, and a short chat completion returned over the pod IP from `golyat-4`.

## Change

- Explicitly pass `-fa on` for the `local-llm` `llama-server` container.
- Keep `turbo3` V cache enabled.

## Verification

- Render `argoproj/local-llm` with `kubectl kustomize`.
- Server dry-run the rendered manifest.
- Smoke-tested one-off `llama-fa-test` pod on `golyat-4` with the same args plus `-fa on`.
