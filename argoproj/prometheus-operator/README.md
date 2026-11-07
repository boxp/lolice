# prometheus-operator

## grafanaのport-forward

```
kubectl port-forward svc/grafana -n monitoring 3000:3000
```

## AI Ops 成果の監視

`ai-ops-outcome-rules.yaml` は成果の到達率と burn rate を記録し、成果の悪化と計測不調を別々に通知します。到達率は予算内に届いた割合で、遅れて届いた作業は失敗に数えます。暫定目標は到達率 90%、burn の暫定閾値は 7 日で 2、28 日で 1 です。固定課題の対照評価で劣化の疑いが出ると `AiOpsHarnessEvalRegression` を通知します。最低観測数を満たさない場合は成果の悪化を通知しません。正式な SLO ではありません。

テストは `argoproj/prometheus-operator/tests/run-promtool-tests.sh` で実行します。ホストの promtool、または Docker と `prom/prometheus:v3.9.1` を使用します。

## provider 利用枠の監視

`ai-provider-quota-rules.yaml` は Claude の利用枠の不足を `AiProviderQuotaExhausted`（`scope=shared` または `scope=model`）で、観測の不足を `AiProviderQuotaObservationStale`（`reason=window_stale`、`window_expired`、`source_unavailable`）で示します。成果 SLO の burn rate とは別の入力です。severity は `info` で、Alertmanager からメールを送りません。エージェントは run の開始時に読みます。定義は `quota/v1-draft` で、観測の古さの閾値 24 時間は暫定です。

## 利用枠の reset 前枯渇の予測（BOXP-462）

`ai-subscription-forecast-rules.yaml` は、各 provider の利用枠（`provider`/`name`/`limit`/`window` ごと。共有枠と専用枠は足さない）が reset 前に尽きるかを予測します。定義は `quota-forecast/v1-draft` で、閾値はすべて暫定です。

- 判断の式: 残量 R = 100 - used、残時間 T = reset - now、最近の速度 v [pt/h]。burn ratio = v × T / (R - 予備 5pt)。1 以上なら、この速度のまま reset 前に尽きます。週の経過率や固定の残率では判断しません。
- 速度は 7d 窓に対して短期 6h と長期 24h。差分は同じ reset 窓の中だけで取り、reset 時刻が変わった区間や used が下がった区間では速度を出しません。窓の開始から 24h 未満のときは長期の速度を「開始からの平均」で代用し、6h 未満は判断しません。
- pod ラベルは集約で落とし、2 系列が重なるときは観測時刻が新しい系列を採ります（再起動で `for` が reset しない）。
- `AiSubscriptionWindowExhaustionForecast`（warning、`reason=sustained_burn`）: 長期と短期の両方で burn ratio ≥ 1、長期の速度 ≥ 0.125pt/h、観測が 2h 以内。`for: 30m`、`keep_firing_for: 1h`。
- `AiSubscriptionWindowExhaustionImminent`（critical）: `reason=fast_exhaustion` は短期の速度で 24h 以内かつ reset 前に尽きる（6h で 2pt 以上の増加）。`reason=exhausted` は used = 100（status を出さない provider だけ。Claude の rejected は `AiProviderQuotaExhausted` が扱う）。同じ枠の warning は Alertmanager の inhibit で止めます。
- `AiSubscriptionWindowForecastUnavailable`（info）: `stale`（観測が 2h 超）、`not_provided`（観測元が窓を返さない）、`unsupported_window`（7d 以外の窓は未監視）、`source_unavailable`（Claude 以外の観測元）。不明を消費ゼロとも利用可能とも読みません。

### Hermes への転送

Alertmanager は label `notify_hermes="true"` を持つアラートの写しを Hermes の webhook route `alertmanager` へ送ります（`continue: true` なので既存のメール経路はそのまま）。route は deliver_only で LLM を起動せず、`alertmanager_relay.clj` が payload を検証・無害化して Telegram へ短く届けます。認証は route 専用 token を `X-Gitlab-Token` で送る方式で、token は hermes-agent namespace で生成し、monitoring の SecretStore `hermes-webhook-token` が 1 つの Secret だけ読みます。通知済みの記録は Alertmanager の PVC（nflog）に残ります。静音時間（02:00-09:00 JST）は critical を含まない通知を出さず、`repeat_interval: 6h` で再送されます。

テストは `tests/run-promtool-tests.sh`（ルール）と `tests/chainsaw/hermes-agent/alertmanager-relay`（kind 上の Alertmanager → 受信口 → 整形）です。
