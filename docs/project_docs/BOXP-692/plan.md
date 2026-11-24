# BOXP-692 長期保持 段階1 実施計画

正本: Obsidian `Projects/prometheus-loki-long-retention/README.md`（BOXP-691 調査）段階1。Owner 承認済み（BOXP-691 通知への回答 A）。

## 範囲

- Loki: `retention_period` を 90 日（2160h）にし、compactor の削除処理（`retention_enabled`）を同じ変更で有効化する。PVC 100Gi は維持。
- Prometheus: 副本を `bulk` タグの大容量ディスクへ移したあと、PVC 120Gi・`retention: 60d`・`retentionSize: 100GB` に変更する。
- 対象外: Loki の容量ひな形（values 60Gi / live 50Gi / 実 PVC 100Gi）の不整合、Prometheus 90 日・200Gi、PVC/PV/データの削除・縮小、別 volume、Longhorn 全体設定の変更。

## 手順（PR 単位）

1. Loki 90 日保持（`argoproj/loki/helm/values.yaml`）。
2. Prometheus volume の副本移動（`argoproj/longhorn/prometheus-replica-move.yaml`、Sync hook Job）。
   - volume に `diskSelector: [bulk]` を設定し、`numberOfReplicas` を一時的に 3 にする。
   - bulk ディスク上に健全な副本（engine から見て RW）が 2 つそろってから、golyat-1 内蔵ディスク上の副本 1 つだけを削除し、`numberOfReplicas` を 2 に戻す。
   - 健全副本が 2 未満になる時間を作らない。対象副本が既に無い場合は設定の確認だけで終わる（冪等）。
3. Prometheus PVC 120Gi（`argoproj/prometheus-operator/prometheus-k8s-db-pvc.yaml`）と副本移動 Job の後片付け。
4. Prometheus 60 日・retentionSize 100GB・volumeClaimTemplate 120Gi（`overlays/prometheus.yaml`）。PVC のオンライン拡張と Pod 再起動が重ならないよう 3 と分ける。
   - Prometheus CR の storage 変更で operator が StatefulSet を作り直す（Pod 再起動、PVC は `whenDeleted: Retain` で保持）。

## 確認

- Loki: `/config` で retention 有効・2160h、`loki_compactor_apply_retention_last_successful_run_timestamp_seconds` が 0 から更新、古いログ（42 日前）が引き続き検索できる。
- 副本: Longhorn replica の配置（golyat-1 内蔵に無い）、volume healthy、2 副本。
- Prometheus: PVC/filesystem 120Gi、`prometheus_tsdb_retention_limit_seconds=5184000`、`..._limit_bytes=100e9`、scrape 継続、Argo Synced/Healthy。
- 継続観測（1 か月）: Prometheus 使用量・取り込み・クエリ負荷・Longhorn 各ディスク実空き → 90 日/200Gi の判断材料。
