# BOXP-179 実装計画

## 目的

control-plane の喪失、etcd 冗長性低下、ストレージ I/O 劣化を実際に通知できる Prometheus/Alertmanager 設定へ拡張する。

## 実施内容

1. 既存の NodeReady、etcd、WAL fsync、disk busy rule と Alertmanager routing を確認する。
2. NodeLease、etcd endpoint の warning/critical 段階化、disk read latency、runbook URL、warning receiver を追加する。
3. 実際の PrometheusRule から rule groups を抽出して `promtool check rules` と `promtool test rules` を行う検証スクリプトを追加する。
4. kustomize のレンダリングと alert 名の重複を確認し、PR を作成する。
