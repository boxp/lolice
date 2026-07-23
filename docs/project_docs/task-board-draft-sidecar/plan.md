# Task Board Draft Sidecar

## Goal

Codex workspace Pod の再起動後も Obsidian Task Board の Draft 取り込み watcher が自動で起動するようにする。

## Plan

- `argoproj/codex-workspace/deployment.yaml` に `obsidian-task-board-draft` sidecar を追加する。
- sidecar は既存の home PVC を `/home/boxp` に mount し、vault 上の `Scripts/task-board-draft-watch.sh` を実行する。
- vault sync のタイミングに依存しないよう、watcher script が作成されるまで 5 秒間隔で待つ。
- Obsidian Sync が実行ビットを保持しない場合に備え、sidecar は watcher を `/bin/bash` 経由で起動する。
- systemd / tmux には依存しない。Kubernetes の Pod lifecycle に任せる。

## Verification

- `kubectl kustomize argoproj/codex-workspace`

## Local Notes

- 2026-06-30: この codex-workspace には `kubectl` / `kustomize` が無いため、manifest build は未実行。`git diff --check` は成功。
