# BOXP-100 Novel Board runner deployment plan

## Goal

Deploy the independent Novel Board runner shipped by `boxp/arch` alongside the existing Task Board runner without changing Task Board, Codex cron, or daily novel cron behavior.

## Changes

1. Add one `novel-board-runner` sidecar to `argoproj/codex-workspace/deployment.yaml`.
2. Set `CODEX_WORKSPACE_ROLE=novel-board-runner` so the codex-workspace image entrypoint execs `novel_board_runner.bb loop`; do not duplicate the runner command in the Deployment.
3. Have the root init container normalize `/home/boxp/.novel-board` to UID/GID 1000 and mode `0700`, then mount the shared home PVC so the non-root runner can read the vault and persist private state there.
4. Use a separate root and `CODEX_NOVEL_BOARD_*` environment variables. Reuse only the Pod UID owner and shared credentials required by the installed agent CLIs.
5. Add a preStop hook that records planned shutdown for Novel Board locks.
6. Document rollout, diagnosis, and rollback ordering with the companion `boxp/arch` PR.
7. Mount the Pi model ConfigMap in the Novel sidecar and explicitly select `llama.cpp/gemma4-26b-vision`, so image attachments supplied by the runner reach an image-capable model.
8. Document the title-only Backlog scaffold, template-backed management notes, Board lane rules, and the default human assignee used to stop automatic writing after manual creation.

## Validation

- `kubectl kustomize argoproj/codex-workspace` renders successfully.
- The Argo CD kustomize image override resolves every codex-workspace container to the verified, published `sha-dd41a33` image built from the companion PR head and containing the Novel runner, title-only scaffold, per-lane `#novel-rule` cards, management-note Workflow seed, Pi vision input support, process-shared note update locking, and its root/non-root entrypoint role contract.
- The rendered Deployment contains exactly one `novel-board-runner` container, private root, vault path, Pod UID owner, poll/stale values, resource limits, restricted security context, and home PVC mount.
- The Novel sidecar has no command override and selects the image-owned runner lifecycle with `CODEX_WORKSPACE_ROLE=novel-board-runner`; its init path is owned by UID/GID 1000 with mode `0700`, and the normal workspace container does not set this role.
- During a `Recreate` rollout with an active Novel lock, the planned-shutdown marker does not bypass the heartbeat guard: recovery occurs after the 180-second stale threshold (normally within 210 seconds with the 30-second poll), without launching duplicate work.
- Existing `task-board-runner`, `codex-cron-scheduler`, workspace, and daily novel configuration remain unchanged.
- The Novel sidecar mounts `/etc/pi-agent-config`, sets `PI_CODING_AGENT_DIR`, and selects `llama.cpp/gemma4-26b-vision`; the model declaration keeps `input: ["text", "image"]`.
- The seeded vault contains the Board-level operating rules, `Templates/Novel Management.md`, and `Novels/README.md`; a title-only Backlog card is scaffolded once and remains human-assigned by default.

## Rollout order

The companion `boxp/arch` PR #11014 workflow dispatch published `sha-dd41a33` from head commit `dd41a339522767aa5f803e29f32600a5bb00513f`. This manifest pins that verified image in `.argocd-source-codex-workspace.yaml`; the publishing run is [Build Codex Workspace Image #29149777509](https://github.com/boxp/arch/actions/runs/29149777509), and the verified registry digest is `sha256:0975c8a89331b88d6b77855655e3c7acb3e490f8e1a09b9a24019de7e7edee66`. A later image-updater commit may advance the tag only to another image that also contains `novel_board_runner.bb`, title-only scaffold, per-lane `#novel-rule` cards and the management-note Workflow seed, Pi vision input support, process-shared note update locking, and the root/non-root `CODEX_WORKSPACE_ROLE=novel-board-runner` entrypoint contract.
