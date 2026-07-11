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

## Validation

- `kubectl kustomize argoproj/codex-workspace` renders successfully.
- The Argo CD kustomize image override resolves every codex-workspace container to the verified, published `sha-f7626af` image built from the companion PR head and containing the Novel runner plus its root/non-root entrypoint role contract.
- The rendered Deployment contains exactly one `novel-board-runner` container, private root, vault path, Pod UID owner, poll/stale values, resource limits, restricted security context, and home PVC mount.
- The Novel sidecar has no command override and selects the image-owned runner lifecycle with `CODEX_WORKSPACE_ROLE=novel-board-runner`; its init path is owned by UID/GID 1000 with mode `0700`, and the normal workspace container does not set this role.
- During a `Recreate` rollout with an active Novel lock, the planned-shutdown marker does not bypass the heartbeat guard: recovery occurs after the 180-second stale threshold (normally within 210 seconds with the 30-second poll), without launching duplicate work.
- Existing `task-board-runner`, `codex-cron-scheduler`, workspace, and daily novel configuration remain unchanged.

## Rollout order

The companion `boxp/arch` PR #11014 workflow dispatch published `sha-f7626af` from head commit `f7626afac5a1efdf2467cbf38b77752ce3610738`. This manifest pins that verified image in `.argocd-source-codex-workspace.yaml`; the publishing run is [Build Codex Workspace Image #29144653164](https://github.com/boxp/arch/actions/runs/29144653164), and the verified registry digest is `sha256:72f2b6fff1c27fa057085451788b10c95b98604df54b5d6a1eb420dd5de6968c`. A later image-updater commit may advance the tag only to another image that also contains `novel_board_runner.bb` and the root/non-root `CODEX_WORKSPACE_ROLE=novel-board-runner` entrypoint contract.
