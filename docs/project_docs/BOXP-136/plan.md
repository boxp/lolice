# BOXP-136: Gmail チェック用 Google API CLI 引数修正

## 目的

`google_api.py gmail search` の廃止済み `--max-results` を、現行 CLI が受け付ける `--max` に置換し、メールチェックと朝のレポートを復旧する。

## 実施内容

1. 実行中 pod の `google_api.py gmail search --help` で現行引数を確認する。
2. hermes-agent ConfigMap 内の `email_check.clj` と `morning_report.clj` を `--max 10` に更新する。
3. `morning_report.clj` のブートストラップ版を更新し、既存 PVC でも新しいスクリプトを再配置する。
4. Kubernetes マニフェストと実行中 pod 上の CLI 呼び出しを検証する。

## 検証基準

- `google_api.py gmail search "is:unread" --max 10` が引数エラーなく実行できる。
- ConfigMap と Deployment に `--max-results` が残らない。
- YAML が構文解析できる。
