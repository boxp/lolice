# ログ収集の欠測アラート 対応手順 (BOXP-729)

ルール: `argoproj/prometheus-operator/log-ingestion-gap-rules.yaml`
仕様: Obsidian `Projects/log-ingestion-gap-alert/spec.md`

確認用クエリ（Grafana Explore / Prometheus）:

```promql
# Alloy Pod ごとの送信レート（0 なら Loki に届いていない）
sum by (pod) (rate(loki_write_sent_entries_total{job="grafana-alloy"}[10m]))
# Pod ごとの読取行数・追跡中のファイル数
sum by (pod) (rate(loki_source_file_read_lines_total{job="grafana-alloy"}[10m]))
sum by (pod) (loki_source_file_files_active_total{job="grafana-alloy"})
# 設定の版
count by (sha256) (alloy_config_hash{job="grafana-alloy"})
# Pod とノードの対応
kube_pod_info{namespace="monitoring", pod=~"grafana-alloy-.*"}
```

## AlloyNodeLogDeliveryStalled

そのノードの Alloy だけが Loki に送れていない。

1. 上のクエリで該当 Pod の読取行数を見る。
   - 読取も 0 で files_active が変わらない → loki.source.file が詰まっている（BOXP-728 の型）。
   - 読取はあるが送信 0 → loki.write 側。Alloy のログ（Loki が使えないので `kubectl logs`）で push のエラーを見る。
2. 詰まっている場合は Alloy を再起動する。kubectl で Pod を消せない場合は GitOps で
   `argoproj/grafana-alloy/helm/values.yaml` の `controller.podAnnotations` の値を変えて DaemonSet を順に再起動する（BOXP-728 の手順）。
3. 再起動後、送信 > 0・config hash が他と一致することを確かめる。欠測期間のうちノードに残っているログは再読込で回収される（BOXP-728 では 12〜30%）。

## AlloyLogDeliveryStalledClusterWide

全ノードから届いていない。Loki 側を先に疑う。

1. `kubectl get pods -n monitoring -l app.kubernetes.io/name=loki`、loki-gateway の状態、Alloy のログの push エラー（5xx / 接続失敗）を見る。
2. Loki が正常に戻っても 15 分以上このアラートが続く、または一部ノードだけ戻らない場合は、上の詰まりを疑い Alloy を再起動する。
3. 計画停止のときは Loki を replicas 0 にすればこのアラートは出ない。それ以外の計画作業は Alertmanager で silence を入れる。

## AlloyConfigNotApplied

1 Pod だけ設定の再読込が止まっている。

1. `alloy_config_last_load_successful` が 0 なら設定の誤り（その Pod だけ失敗するのはノード依存の設定、例: journal のパス）。
2. 1（成功）なのに古い hash のままなら再読込が詰まっている。送信・読取も止まっていないか上のクエリで確かめ、Alloy を再起動する。
