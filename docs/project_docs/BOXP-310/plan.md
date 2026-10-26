# BOXP-310: prometheus-operator Application の自動 prune 恒久化

## 目的

Git から削除済みの監視リソース（例: 2026-10-26 に手動削除した PrometheusRule
`stable-diffusion-observability-rules`）が本番に残骸として残り、不要な FIRING を
継続させる再発を防ぐため、Argo CD Application `prometheus-operator` の
`spec.syncPolicy.automated.prune` を `true` にする。

Owner 承認（BOXP-310 Notes 2026-10-26 05:21 UTC）: 本 Application に限り
自動 prune の恒久有効化を承認。他 Application やクラスタ共通既定値へは広げない。

## 変更内容

- `argoproj/prometheus-operator/application.yaml` に `syncPolicy.automated.prune: true` を追加（最小差分）。
- `selfHeal` / `allowEmpty` / `syncOptions` は変更しない。
  - live Application は既に `automated: {prune: false, selfHeal: false}` で自動同期中
    （直近 sync は `initiatedBy.automated=true`）。Git 側は 2024-04-23 の
    commit 966e06f `remove: prune` 以降 `automated` ブロック自体が無く、
    live との差は client-side 3-way diff の性質上 Synced 扱いだった。
  - 本変更後の実効値は `automated: {prune: true, selfHeal: false}` となり、
    selfHeal の挙動は変わらない。

## 安全確認（変更前）

- `kustomize build argoproj/prometheus-operator`（origin/main 898caae）の render 140 件と
  live Application inventory 140 件が group/kind/name で完全一致。
  `status.resources[].requiresPruning` は 0 件、OutOfSync 0 件。
  つまり prune 有効化時点で削除されるリソースは無い。
- 共有監視（critical rule 40 本、`count(up==0)=0`）の変更前スナップショットを
  Obsidian `Projects/alert-pipeline-integrity-triage/evidence-20261026/` に保存。

## 検証手順（merge 後）

1. 親 Application `argocd-apps`（selfHeal: true）が live Application へ `prune: true` を反映することを確認。
2. `prometheus-operator` Application が Synced/Healthy を維持し、`requiresPruning` が 0 件のままであることを確認。
3. Prometheus 実効 rule 数 / critical rule 数 / `count(up==0)` / firing 一覧が変更前と同一であることを確認。
4. 結果を Obsidian `Projects/alert-pipeline-integrity-triage/residual-firing-remediation.md` と BOXP-310 Notes に記録。
