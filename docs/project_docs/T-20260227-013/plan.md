# T-20260227-013: GPT-5.3-Codex-Spark model support

## Background

GPT-5.3-Codex-Spark has become available on ChatGPT Plus subscriptions.
This task adds the model to the OpenClaw ConfigMap so agents can use it
via the `codex-spark` alias.

## Scope

- Add `openai-codex/gpt-5.3-codex-spark` to the model alias map in
  `argoproj/openclaw/configmap-openclaw.yaml`
- Alias: `codex-spark`
- Provider: `openai-codex` (OAuth / ChatGPT Plus subscription)

## Design decisions

- Follows the existing `openai-codex/gpt-5.3-codex` pattern (no LiteLLM
  entry needed; OAuth-based models are managed by OpenClaw directly)
- Default model (`primary`) is unchanged (`openai-codex/gpt-5.3-codex`)
- No changes to LiteLLM ConfigMap (`configmap-litellm.yaml`)
- No changes to fallback chain or heartbeat model

## Usage

After deployment, agents can select the model with:

```
codex-spark
```

or by its full qualified name:

```
openai-codex/gpt-5.3-codex-spark
```

## Files changed

- `argoproj/openclaw/configmap-openclaw.yaml` - Added model alias entry
- `docs/project_docs/T-20260227-013/plan.md` - This plan document
