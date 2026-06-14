# codex-cron-obsidian-root: Codex cron root を Obsidian vault に寄せる

## Goal

Codex workspace cron scheduler sidecar が `~/.codex-cron` symlink ではなく、Obsidian vault 上の実体 `/home/boxp/Documents/obsidian-headless/BOXP/Infrastructure/Codex Cron` を直接読むようにする。

## Plan

1. `argoproj/codex-workspace/deployment.yaml` の `CODEX_CRON_ROOT` を Obsidian vault path に変更する。
2. `argoproj/codex-workspace/cron.md` の運用説明と registry 例を同じ path に更新する。

## Validation

- `kubectl kustomize argoproj/codex-workspace`
