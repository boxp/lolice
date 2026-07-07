# BOXP-58: Hermes Agent custom image

## Goal

Use the `boxp/arch` custom image `ghcr.io/boxp/arch/hermes-agent` so the Hermes Agent Pod no longer installs Obsidian headless, Babashka, or the Task Board skill at every startup.

## Image contents

- Base image: `docker.io/nousresearch/hermes-agent@sha256:94f90d3cb66c848e6d7465fb7ca11dc485f096700edc26f571fedab59f4274f7`.
- Added runtime tools:
  - `obsidian-headless@0.0.12` under `/opt/obsidian-headless`.
  - `babashka@1.12.218` as `/usr/local/bin/bb`.
  - BOXP Obsidian Task Board skill under `/opt/boxp/hermes-agent/skills/obsidian-task-board`.
- Static tooling stays image-owned; shared mutable state stays on the Hermes PVC.

## lolice changes

- `argoproj/hermes-agent/deployment.yaml` now uses `ghcr.io/boxp/arch/hermes-agent:latest` for:
  - `bootstrap-config` initContainer;
  - `hermes-agent` main container;
  - `obsidian-sync` sidecar.
- Removed initContainers:
  - `install-obsidian-headless`;
  - `install-babashka`;
  - `install-obsidian-task-board-skill`.
- Removed the Task Board skill ConfigMap generator because the skill is baked into the custom image.
- Added `argoproj/argocd-image-updater/imageupdaters/hermes-agent.yaml` so ImageUpdater tracks only this self-built GHCR image.

## Remaining initContainer work

`bootstrap-config` remains because it owns mutable PVC state, not image contents:

- create `/opt/data/Documents/obsidian-headless/BOXP`;
- perform the one-time `1000:10000` shared ownership migration;
- seed `/opt/data/config.yaml` only if it is absent;
- seed `/opt/data/skills/note-taking/obsidian-task-board` only if it is absent;
- set group-write permissions on shared directories.

## Rollback

1. Revert `argoproj/hermes-agent/deployment.yaml` to the prior official Hermes image plus Node/Babashka/skill initContainers.
2. Restore the `hermes-agent-obsidian-task-board-skill` ConfigMap generator and source files.
3. Remove `argoproj/argocd-image-updater/imageupdaters/hermes-agent.yaml` from the ImageUpdater kustomization.
4. Sync `argoproj/hermes-agent` in Argo CD.

## Verification

- `kubectl kustomize argoproj/hermes-agent`
- `kubectl kustomize argoproj/argocd-image-updater`
- After `boxp/arch` publishes the image:
  - `kubectl -n hermes-agent rollout status deploy/hermes-agent`;
  - `kubectl -n hermes-agent exec deploy/hermes-agent -c obsidian-sync -- ob --version`;
  - `kubectl -n hermes-agent exec deploy/hermes-agent -c hermes-agent -- bb --version`;
  - verify Web UI through Cloudflare Access;
  - verify local LLM and Obsidian vault read/write paths.
