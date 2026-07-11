# BOXP-100 Novel Board runner deployment plan

## Goal

Deploy the independent Novel Board runner shipped by `boxp/arch` alongside the existing Task Board runner without changing Task Board, Codex cron, or daily novel cron behavior.

## Changes

1. Add one `novel-board-runner` sidecar to `argoproj/codex-workspace/deployment.yaml`.
2. Set `CODEX_WORKSPACE_ROLE=novel-board-runner` so the codex-workspace image entrypoint execs `novel_board_runner.bb loop`; do not duplicate the runner command in the Deployment.
3. Mount the shared home PVC so the runner can read the vault and persist private state under `/home/boxp/.novel-board`.
4. Use a separate root and `CODEX_NOVEL_BOARD_*` environment variables. Reuse only the Pod UID owner and shared credentials required by the installed agent CLIs.
5. Add a preStop hook that records planned shutdown for Novel Board locks.
6. Document rollout, diagnosis, and rollback ordering with the companion `boxp/arch` PR.

## Validation

- `kubectl kustomize argoproj/codex-workspace` renders successfully.
- The Argo CD kustomize image override resolves every codex-workspace container to the verified, published `sha-6fa3915` image built from the companion PR head and containing the Novel runner plus its entrypoint role contract.
- The rendered Deployment contains exactly one `novel-board-runner` container, private root, vault path, Pod UID owner, poll/stale values, resource limits, restricted security context, and home PVC mount.
- The Novel sidecar has no command override and selects the image-owned runner lifecycle with `CODEX_WORKSPACE_ROLE=novel-board-runner`; the normal workspace container does not set this role.
- During a `Recreate` rollout with an active Novel lock, the planned-shutdown marker does not bypass the heartbeat guard: recovery occurs after the 180-second stale threshold (normally within 210 seconds with the 30-second poll), without launching duplicate work.
- Existing `task-board-runner`, `codex-cron-scheduler`, workspace, and daily novel configuration remain unchanged.

## Rollout order

The companion `boxp/arch` PR #11014 workflow dispatch published `sha-6fa3915` from head commit `6fa3915bade8482e6c51eaf911e92d8266ed84bf`. This manifest pins that verified image in `.argocd-source-codex-workspace.yaml`; the publishing run is [Build Codex Workspace Image #29143955512](https://github.com/boxp/arch/actions/runs/29143955512), and the verified registry digest is `sha256:9013eaff919b24c14e88636c28fdd27d432963eeb685836666c060255e7dd053`. A later image-updater commit may advance the tag only to another image that also contains `novel_board_runner.bb` and the `CODEX_WORKSPACE_ROLE=novel-board-runner` entrypoint contract.
