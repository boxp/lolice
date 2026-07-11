# BOXP-100 Novel Board runner deployment plan

## Goal

Deploy the independent Novel Board runner shipped by `boxp/arch` alongside the existing Task Board runner without changing Task Board, Codex cron, or daily novel cron behavior.

## Changes

1. Add one `novel-board-runner` sidecar to `argoproj/codex-workspace/deployment.yaml`.
2. Run `/opt/codex-workspace/novel-board/novel_board_runner.bb loop` from the codex-workspace image.
3. Mount the shared home PVC so the runner can read the vault and persist private state under `/home/boxp/.novel-board`.
4. Use a separate root and `CODEX_NOVEL_BOARD_*` environment variables. Reuse only the Pod UID owner and shared credentials required by the installed agent CLIs.
5. Add a preStop hook that records planned shutdown for Novel Board locks.
6. Document rollout, diagnosis, and rollback ordering with the companion `boxp/arch` PR.

## Validation

- `kubectl kustomize argoproj/codex-workspace` renders successfully.
- The rendered Deployment contains exactly one `novel-board-runner` container, private root, vault path, Pod UID owner, poll/stale values, resource limits, restricted security context, and home PVC mount.
- Existing `task-board-runner`, `codex-cron-scheduler`, workspace, and daily novel configuration remain unchanged.

## Rollout order

Merge and publish the companion `boxp/arch` image first. After image updater has selected an image containing `novel_board_runner.bb`, merge this manifest PR. This avoids starting a sidecar with a path absent from the current image.
