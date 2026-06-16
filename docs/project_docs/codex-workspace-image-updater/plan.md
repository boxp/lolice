# Codex workspace image updater

## 背景

`argoproj/codex-workspace/deployment.yaml` は `ghcr.io/boxp/arch/codex-workspace:latest` を `workspace`、`obsidian-sync`、`codex-cron-scheduler` の複数コンテナで利用している。

`imagePullPolicy: Always` だけでは Deployment の Pod template が変わらないため、`latest` が更新されても既存 Pod が再作成されず、Codex workspace の Docker image 更新が反映されないことがある。

## 方針

- Argo CD Image Updater の CRD 管理対象に `codex-workspace` Application を追加する。
- 他の workload と同じく、registry 上で最も新しい build tag を選ぶため、update strategy は `newest-build` にする。
- `manifestTargets.kustomize.name` で `ghcr.io/boxp/arch/codex-workspace` を指定し、同一 image を使う複数コンテナをまとめて更新対象にする。
- `codex-workspace` は amd64 node に固定されているため、ImageUpdater 側も `linux/amd64` を対象 platform とする。
- `argocd-image-updater` の registry config に `ghcr.io` を明示的に追加する。

## 変更対象

- `argoproj/argocd-image-updater/imageupdaters/codex-workspace.yaml`
- `argoproj/argocd-image-updater/imageupdaters/kustomization.yaml`
- `argoproj/argocd-image-updater/overlays/configmap.yaml`

## 検証

- [x] Babashka + `clj-yaml` で変更 YAML の構文確認
- [ ] `kubectl kustomize argoproj/argocd-image-updater`
  - ローカル環境に `kubectl` / `kustomize` が無いため未実行
