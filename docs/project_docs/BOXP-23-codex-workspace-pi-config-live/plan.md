# BOXP-23 codex-workspace pi config live update

## Goal

Make `codex-workspace` consume the updated pi agent `models.json` ConfigMap without pinning an old file through a Kubernetes `subPath` bind mount.

## Context

`local-llm` already runs `gemma4-26b` and `ornith-35b` with `ctx-size = 65536`, `--models-max 1`, Turbo3 KV cache, and the Ooedo/Lunar batch settings. The live `codex-workspace` Pod still saw the old pi config because `/home/boxp/.pi/agent/models.json` was mounted as a single ConfigMap key via `subPath`. Kubernetes does not live-update existing subPath file mounts, so the ConfigMap volume had the new file while the container still read the old bind-mounted copy.

## Plan

1. Mount the `pi-config` ConfigMap as a directory at `/etc/pi-agent-config` instead of directly over `/home/boxp/.pi/agent/models.json`.
2. During the existing `fetch-ssh-keys` initContainer, replace `/home/boxp/.pi/agent/models.json` with a symlink to `/etc/pi-agent-config/models.json`.
3. Keep the rest of `/home/boxp/.pi/agent` on the persistent home volume so `auth.json`, sessions, settings, hooks, skills, and extensions are not hidden by a ConfigMap directory mount.

## Validation

- `kubectl kustomize argoproj/codex-workspace`
- `git diff --check`
