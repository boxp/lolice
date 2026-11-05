# BOXP-212 実装計画

## 目的

BOXP-211 で発覚した「Longhorn の disk が Schedulable=False でも Prometheus に気づける仕組みが無い」状態を解消する。Longhorn メトリクスの収集を確認し、disk 逼迫・ボリューム肥大・収集停止を検知する PrometheusRule と promtool テストを追加する。

## 実施内容

1. 収集確認（read-only）: Grafana の datasource proxy 経由で Prometheus を照会し、`longhorn_*` が入っていること、`up{job="longhorn-backend"}` が 4 target とも 1 であることを確認する。
2. 設計確定: 各アラート式を実データで評価し、現状で誤発火しないことを確認する。
3. 実装:
   - `argoproj/prometheus-operator/longhorn-storage-rules.yaml` を追加し `kustomization.yaml` に登録する。
   - アラート: `LonghornDiskNotSchedulable`（warning, 15m）、`LonghornDiskUsageHigh`（warning, 80%, 30m）、`LonghornDiskUsageCritical`（critical, 90%, 10m）、`LonghornVolumeActualSizeDiverged`（warning, 3 倍かつ 20GB 超, 6h）、`LonghornMetricsAbsent`（warning, 30m）。
   - `argoproj/prometheus-operator/tests/longhorn-storage-rules.test.yaml` を追加する（Schedulable=False、disk 85% / 95%、Loki 51.4GB vs 2.3GB の乖離、比例している大容量ボリュームは鳴らない、収集停止、正常時は鳴らない）。
   - `scripts/test-prometheus-rules.sh` を tests/ 配下の全ペアを回す形に拡張する。
   - `docs/project_docs/BOXP-212/runbook.md` に各アラートの確認手順を書き、`runbook_url` から参照する。
4. 検証: `kustomize build argoproj/prometheus-operator` と `scripts/test-prometheus-rules.sh` をローカルで pass させ、codex review の上で PR を作成する。
5. 反映確認（merge 後、別 run）: Application prometheus-operator が Synced / Healthy、Prometheus の Rules 画面に `longhorn-storage.rules` が載り全アラートが inactive であることを確認する。
6. Application longhorn の ExternalSecret OutOfSync は diff を確認して Notes に記録し、本チケットでは修正しない。

## 設計判断

- 集約: `longhorn_disk_status` は `condition_reason` ラベルが状態で変わるため `max by (node, disk)`。disk 使用率も `max by (node, disk)` で両辺を揃え、アラートラベルを node / disk だけにする。
- volume 系メトリクスはレプリカを持つ各ノードの manager が出すため `max by (pvc_namespace, pvc)`。kubelet 側も再 attach 時に複数ノードから出る瞬間があるため同じく `max by` で一意にし、one-to-one マッチングのエラーを避ける。
- 乖離アラートの閾値は設計案どおり 3 倍 + 20GB、for 6h。実測では比率が 1000 倍超の小容量ボリューム（alertmanager 52MB）があり、20GB 条件が無いと誤発火するため二重条件を維持する。
- Longhorn 1.9.1 は Allocated（予約量）をメトリクスに出さないため、予約超過は `longhorn_disk_status{condition="schedulable"}` でのみ検知する。
- CI への promtool 組み込みは本チケットでは行わず、ローカル実行結果を PR に貼る。
