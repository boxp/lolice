# prometheus-operator

## grafanaのport-forward

```
kubectl port-forward svc/grafana -n monitoring 3000:3000
```

## AI Ops 成果の監視

`ai-ops-outcome-rules.yaml` は成果の到達率と burn rate を記録し、成果の悪化と計測不調を別々に通知します。到達率は予算内に届いた割合で、遅れて届いた作業は失敗に数えます。暫定目標は到達率 90%、burn の暫定閾値は 7 日で 2、28 日で 1 です。固定課題の対照評価で劣化の疑いが出ると `AiOpsHarnessEvalRegression` を通知します。最低観測数を満たさない場合は成果の悪化を通知しません。正式な SLO ではありません。

テストは `argoproj/prometheus-operator/tests/run-promtool-tests.sh` で実行します。ホストの promtool、または Docker と `prom/prometheus:v3.9.1` を使用します。
