# BOXP-23 codex-workspace local LLM pi config

## Background

`local-llm` now exposes a verified `gemma4-26b` alias through `llama-server.local-llm.svc.cluster.local:8080`.

The Phase 6 end state requires the existing `codex-workspace` Pod to use this local LLM through pi agent. The current NetworkPolicy blocks egress to `local-llm`, and `~/.pi/agent/models.json` is not managed by GitOps.

## Plan

- Add `codex-workspace-pi-config` ConfigMap with `models.json`.
- Mount the config to `/home/boxp/.pi/agent/models.json` in the `workspace` container.
- Allow egress from `codex-workspace` to `local-llm` pods on TCP 8080.
- Keep image digest updates under the existing `argocd-image-updater` flow.

## Validation

- `kubectl kustomize argoproj/codex-workspace`
- `kubectl apply --dry-run=server`
- After the `codex-workspace` image contains `pi`, verify:
  - `curl http://llama-server.local-llm.svc.cluster.local:8080/health`
  - `pi --version`
  - `pi` can call `gemma4-26b` and return `OK`
