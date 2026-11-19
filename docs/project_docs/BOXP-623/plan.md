# BOXP-623: アラートの棚卸しと Grafana への運用入口の統合

## 方針（Owner 承認済み）

- 運用の入口は Grafana に統一する。
- 判定の正本は当面 PrometheusRule と GitOps（このリポジトリ）を維持する。
- 通知経路は Alertmanager（メール / Hermes）を維持する。Grafana が止まっても判定と通知は変わらない。
- 全面的な Grafana 管理ルールへの移行、二重評価、二重通知はしない。

## 棚卸し（2026-11-19 時点、read-only 照会）

### Prometheus 側（正本）

- PrometheusRule 15 件（すべて monitoring namespace、`ruleNamespaceSelector: {}`）。評価 health 異常 0。
  - kube-prometheus 同梱: alertmanager / kube-prometheus / kube-state-metrics / kubernetes-monitoring / node-exporter / prometheus / prometheus-operator / grafana
  - このリポジトリ独自: control-plane-node（ノード NotReady・etcd quorum・read-only FS ほか）、longhorn-storage、local-llm-gpu-observability、ai-ops-outcome、ai-provider-quota、ai-subscription-forecast、aws-cost
- 取得対象 23 job、up=0 は 0。
- 発火中（照会時）: warning 2（KubeCPUOvercommit / KubeMemoryOvercommit）、info 13（CPUThrottlingHigh、InfoInhibitor で抑制中）、pending 少数。

### Alertmanager（通知の正本）

- `critical` → メール + Hermes（写し）。`notify_hermes="true"` → Hermes（写し）。`warning` → メール（15 分待ち、7 日ごと再送）。
- `info`・Watchdog・InfoInhibitor → 通知しない（Default / Watchdog / null）。
- inhibit: critical→warning/info、warning→info、利用枠 Imminent→Forecast、InfoInhibitor→info。
- 外部の dead man's switch はない（Watchdog の受け手が空）。→ 未対応として残す。

### Grafana 側

| 対象 | 状態 | 判断 |
|---|---|---|
| ルール `hitohub-frontend state running count` | 停止中、対象 namespace `prod-hitohub` は BOXP-226 で廃止済み | 削除（JSON を退避してから） |
| ルール `hitohub-backend state running count` | 同上 | 削除（同上） |
| ルール `ark asa server restarts count` | 停止中、ark-server は現存 | 維持（再開するかは Owner 判断） |
| ルール `onitiku palworldが稼働していません` | 有効、Discord（onitiku server）へ通知 | 維持（Prometheus 側と重複しない独自の Discord 通知） |
| データソース `alertmanager` | UI 作成で URL が空。実際には未接続。NetworkPolicy も未許可 | GitOps で provisioning し接続する |
| contact point `email receiver` | 既定のまま `<example@email.com>`、どこからも使われていない | 残す（無害。整理は任意） |
| 重複ダッシュボード（`Kubernetes / API server Copy`、Node Exporter / Prometheus Overview の UI 複製） | UI 作成 | 削除しない。整理候補として記録 |
| HitoHub Prod / Stage ダッシュボード | 対象サービス廃止済み | 削除しない。整理候補として記録 |

### 欠落・未対応（今回は追加しない）

- バックアップの鮮度監視: Longhorn の backup RecurringJob（毎日 00:00 UTC）が「動かなかった」ことを検知するルールがない（KubeJobFailed は失敗だけ）。
- 外部 dead man's switch: Prometheus / Alertmanager 自体が止まると誰にも届かない。
- palserver-2 には palserver 相当の Discord 通知がない。

## 実装（このリポジトリ）

1. `overlays/grafana-datasources.yaml`: Alertmanager データソースを provisioning（`handleGrafanaManagedAlerts: false`）。
2. `overlays/network-policy-alertmanager.yaml`: Grafana → Alertmanager 9093 を許可。
3. `dashboards/alert-entry.json`（`scripts/alert-entry-dashboard/generate.bb` で生成）: 監視の入口。
   - 件数（critical / warning / info / pending / 抑制中 / silence）
   - Firing / Pending の一覧。行のリンクで「発生 30 分前から」の入口・namespace 負荷・Alertmanager・ルール定義へ
   - 時間帯（state timeline）とエラーらしきログ（Loki）
   - 監視の健全性: Watchdog、ルール評価失敗、取りこぼし、up=0、消えた job、Prometheus→Alertmanager、通知失敗、破棄、設定再読込、Grafana 管理ルールの失敗 / NoData
   - 個別ダッシュボードへのリンク
4. kustomization / grafana Deployment に ConfigMap を追加。

## 確認

- `scripts/alert-entry-dashboard/test.sh`（生成の再現性・ID 重複・重なり・内部アラート除外・Grafana 側アラート定義なし）。
- 全 PromQL を本番 Prometheus に照会し成功を確認。
- merge 後: Argo CD sync、Grafana でダッシュボードとデータソースの health、Alertmanager の通知経路（receiver・送信数）が変わっていないことを確認。
- Grafana の hitohub ルールは API で退避後に削除し、残りのルールの状態を確認。
