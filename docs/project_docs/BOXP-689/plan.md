# BOXP-689: Loki だけ ServerSideDiff を無効化する

## 背景
BOXP-688 の調査（Obsidian `Projects/loki-argocd-sync-unknown/README.md`）で、Argo CD v3.6 の server-side diff が
StatefulSet/loki の volumeClaimTemplates（Git 60Gi / live 50Gi、immutable）を dry-run で拒否し、
`ComparisonError` → Sync Unknown → 自動同期停止になっていることを確認。Owner が Loki のみの限定修復を承認。

## 変更
- `argoproj/loki/application.yaml` に `argocd.argoproj.io/compare-options: ServerSideDiff=false` を追加（理由コメント付き）。
- syncOptions（ServerSideApply=true / RespectIgnoreDifferences=true）・ignoreDifferences は変更しない。

## 対象外
容量設定（values.yaml の 60Gi）整理、PVC 縮小/拡張/削除、StatefulSet 再作成、Loki 再起動、他 Application や Argo CD 全体の比較設定。

## 確認
1. merge 後 argocd-apps が Application を更新し、annotation が付くこと。
2. loki の `ComparisonError` が消え、Sync=Synced / Health=Healthy になること（差分は ignore 済みの volumeClaimTemplates のみ想定）。
3. 自動同期が「Skipping auto-sync」でなくなり、StatefulSet/PVC に意図しない変更が入らないこと（loki-0 の再起動なし、PVC 100Gi のまま）。
4. Loki のログ取り込み・検索が継続していること。
5. 想定外の差分（OutOfSync で破壊的 sync が必要）が出た場合は sync せず報告する。

仕様先行/Lean: 設定 1 行の変更で状態機械として述べる規則がないため対象外。
