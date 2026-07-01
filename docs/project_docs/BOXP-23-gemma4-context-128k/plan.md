# BOXP-23 Gemma4 128k context

## Context

`local-llm` currently exposes `gemma4-26b` with a 65536 token context in both the llama-server model preset and the `codex-workspace` pi model metadata.

Gemma4 should be available with a 128k context window while keeping the existing Ornith 65536 setting unchanged.

## Plan

1. Set the `gemma4-26b` llama-server model preset `ctx-size` to `131072`.
2. Align the llama-server startup `-c` value with the larger Gemma4 context.
3. Update the `codex-workspace` pi config so only `gemma4-26b` advertises `contextWindow=131072`.
4. Validate the YAML and embedded pi `models.json`.

## Verification

- `python - <<'PY' ... yaml.safe_load_all(...) ... PY`
- `python - <<'PY' ... json.loads(models.json) ... PY`
- `kubectl kustomize argoproj/local-llm >/tmp/lolice-local-llm.yaml`
- `kubectl kustomize argoproj/codex-workspace >/tmp/lolice-codex-workspace.yaml`
