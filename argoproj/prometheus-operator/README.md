# prometheus-operator

## grafanaのport-forward

```
kubectl port-forward svc/grafana -n monitoring 3000:3000
```

## AI Ops 成果の監視

`ai-ops-outcome-rules.yaml` は成果の到達率と burn rate を記録し、成果の悪化と計測不調を別々に通知します。暫定目標は到達率 90%、burn の暫定閾値は 7 日で 2、28 日で 1 です。最低観測数を満たさない場合は成果の悪化を通知しません。正式な SLO ではありません。

テストは `argoproj/prometheus-operator/tests/run-promtool-tests.sh` で実行します。ホストの promtool、または Docker と `prom/prometheus:v3.9.1` を使用します。

## provider 利用枠の監視

`ai-provider-quota-rules.yaml` は Claude の利用枠の不足を `AiProviderQuotaExhausted`（`scope=shared` または `scope=model`）で、観測の不足を `AiProviderQuotaObservationStale`（`reason=window_stale`、`window_expired`、`source_unavailable`）で示します。成果 SLO の burn rate とは別の入力です。severity は `info` で、Alertmanager からメールを送りません。エージェントは run の開始時に読みます。定義は `quota/v1-draft` で、観測の古さの閾値 24 時間は暫定です。
