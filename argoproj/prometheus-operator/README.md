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

## agent CLI の再ログイン期限前通知（BOXP-204）

`agent-auth-expiry-rules.yaml`（定義 `agent-auth/v1`）は、agent CLI の手動再ログインが必要になる前に Owner へ知らせます。入力は codex-workspace の task-board runner が毎分書く `ai-ops/agent-auth.prom`（arch `docker/codex-workspace/task-board/agent_auth_expiry.bb`）で、期限の時刻・最終更新時刻・enum だけです。token や account 情報は含みません。

| provider | 期限の根拠 | 通知 |
| --- | --- | --- |
| Claude Code | 認証ファイルの `refreshTokenExpiresAt`（refresh token の期限 = 過ぎると再ログインが必要）。access token の `expiresAt` は CLI が自動更新するので通知しない | `AgentAuthReloginDue`（残り 72h 以下）→ `AgentAuthReloginImminent`（24h 以下）→ `AgentAuthReloginExpired` |
| Codex | refresh token の期限は非公開（unknown、推測しない）。`last_refresh` だけを読む | `AgentAuthRefreshStalled`（10 日自動更新がない = 未使用か更新失敗） |
| Antigravity (agy) | 認証を読まない（BOXP-630 の契約）。期限は unknown | `AgentAuthRunLoginFailed`（最後の run がログイン失敗。次の成功 run で解除） |

`AgentAuthObservationUnavailable` は期限を読めないとき（`incomplete` = 期限・更新時刻の項目が欠落か解析不能、`missing`/`unreadable`/`unsupported`/`policy_disallowed`）と観測が 2h 以上止まったときです。不明を問題なしとは読みません。利用枠の不足は `AiProviderQuota*`、通信や CLI の一時失敗はこのルールの対象外です。

配送は Alertmanager の `component="agent-auth"` route で Hermes（家の受信箱と Telegram）へ送り、同じ通知の再送は 1 日 1 回まで、解除（再ログイン後の回復）も送ります。severity は warning で、静音時間（02:00-09:00 JST）は送らず朝に届きます。72h の余裕は、週末や外出で 1 日以上ログインできない場合でも 24h 帯までにもう一度知らせるためです。閾値は暫定です。再ログイン後の確認は codex-workspace で `bb /opt/codex-workspace/task-board/agent_auth_expiry.bb status`。

本人ログインで解消し得る通知（期限 72h/24h/期限切れ・更新停滞・run のログイン失敗・メタデータを読めない）には annotation `reauth_url` で BOXP-634 の本人専用ページ `https://codex-auth.b0xp.io/`（Cloudflare Access で Owner のみ）を付けます。Hermes の整形（`argoproj/hermes-agent/configmap.yaml` の `alertmanager_relay.clj`）は本文中の URL を省略するため、`reauth_url` が許可リストと完全一致するときだけ発生中の通知に「再認証: URL」行を出します。各 provider の公式ログイン URL は都度の一時コード入りなので通知には載せません（ページで CLI を選ぶと公式ログインが始まります）。観測停止（observation_stale）はログインで直らないため付けません。

## 利用枠の reset 前枯渇の予測（BOXP-462）

`ai-subscription-forecast-rules.yaml` は、各 provider の利用枠（`provider`/`name`/`limit`/`window` ごと。共有枠と専用枠は足さない）が reset 前に尽きるかを予測します。定義は `quota-forecast/v1-draft` で、閾値はすべて暫定です。

- 判断の式: 残量 R = 100 - used、残時間 T = reset - now、最近の速度 v [pt/h]。burn ratio = v × T / (R - 予備 5pt)。1 以上なら、この速度のまま reset 前に尽きます。週の経過率や固定の残率では判断しません。
- 速度は 7d 窓に対して短期 6h と長期 24h。差分は同じ reset 窓の中だけで取り、reset 時刻が変わった区間や used が下がった区間では速度を出しません。窓の開始から 24h 未満のときは長期の速度を「開始からの平均」で代用し、6h 未満は判断しません。
- pod ラベルは集約で落とし、2 系列が重なるときは観測時刻が新しい系列を採ります（再起動で `for` が reset しない）。
- `AiSubscriptionWindowExhaustionForecast`（warning、`reason=sustained_burn`）: 長期と短期の両方で burn ratio ≥ 1、長期の速度 ≥ 0.125pt/h、観測が 2h 以内。`for: 30m`、`keep_firing_for: 1h`。
- `AiSubscriptionWindowExhaustionImminent`（critical）: `reason=fast_exhaustion` は短期の速度で 24h 以内かつ reset 前に尽きる（6h で 2pt 以上の増加）。`reason=exhausted` は used = 100（status を出さない provider だけ。Claude の rejected は `AiProviderQuotaExhausted` が扱う）。同じ枠の warning は Alertmanager の inhibit で止めます。
- `AiSubscriptionWindowForecastUnavailable`（info）: `stale`（観測が 2h 超）、`not_provided`（観測元が窓を返さない）、`unsupported_window`（7d 以外の窓は未監視）、`source_unavailable`（Claude 以外の観測元）。不明を消費ゼロとも利用可能とも読みません。

### Hermes への転送

Alertmanager は label `notify_hermes="true"` を持つアラートの写しを Hermes の webhook route `alertmanager` へ送ります（`continue: true` なので既存のメール経路はそのまま）。route は deliver_only で LLM を起動せず、`alertmanager_relay.clj` が payload を検証・無害化し、通知用 channel plugin `boxp-notify` が家の受信箱と Telegram へ届けます。relay 自体は写しを保存しません。認証は route 専用 token を `X-Gitlab-Token` で送る方式で、token は hermes-agent namespace で生成し、monitoring の SecretStore `hermes-webhook-token` が 1 つの Secret だけ読んで monitoring の Secret `alertmanager-hermes-webhook-token` に写し、Alertmanager CR の `spec.secrets` でマウントしたファイルを `http_headers` の `files:` で読みます（`secrets:` は prometheus-operator が設定を再生成するときに `<secret>` へ置き換えるため使えません）。通知済みの記録は Alertmanager の PVC（nflog）に残ります。静音時間（02:00-09:00 JST）は critical を含まない通知を出さず、`repeat_interval: 6h` で再送されます。

テストは `tests/run-promtool-tests.sh`（ルール）と `tests/chainsaw/hermes-agent/alertmanager-relay`（kind 上の Alertmanager → 受信口 → 整形）です。

### BOXP-463: Gemini API 前払い残高

`AI / Gemini API 前払い残高` ダッシュボードは Owner が AI Studio Billing タブで確認した基準と、この環境の利用 ledger に基づく推計を表示する。実残高は Google 公式 API がなく取得不能。基準入力 `baseline.json` と `usage.jsonl` の形式および更新方法は [codex-workspace README](../codex-workspace/README.md) を参照。購入・付与・失効後は Owner が再確認し agent が基準を更新する。他 client・project の消費、課金遅延、購入・付与・失効や価格改定は推計に反映されない。未取得はゼロではない。 stat パネルは instant query・最新値のみで表示し、現在値が消えた場合に過去の残額や ok 状態を表示し続けない。

推奨アラート（今回は未配備）: 推計残額 5 USD 未満、基準が7日超、推計状態が ok 以外、sidecar 不在。

## AWS コストの読み方

取得から36時間を超えた場合、または `aws_cost_snapshot_available` が 0 の場合は不明として扱います。金額の欠測を 0 と読まないでください。`aws_cost_estimated` が 1 の期間は見込、0 は確定です。月末予測は見込です。

S3 の使用タイプ別（BOXP-598）: `aws_cost_s3_usage_type_day_usd{usage_type,days_ago}` は直近 14 日の日額、`aws_cost_s3_usage_type_day_quantity` は同じ使用量（単位は使用タイプごと、例 `TimedStorage-ByteHrs` は GB-月）。`aws_cost_s3_usage_available` が 0 または系列が無いときは不明で、0 円ではありません。費用上位 10 種以外は `Other` にまとめ、`Other` の使用量は出しません。

## 監視の入口（BOXP-623）

Grafana の `監視の入口：アラートと監視の健全性`（`/d/alert-entry/`）が運用の入口です。判定の正本は PrometheusRule、通知の正本は Alertmanager（メール / Hermes）のままで、この画面は読むだけです。Grafana が止まっても通知は届きます。

- 通知を受けたら `/d/alert-entry/?var-alertname=<alertname>&var-namespace=<namespace>` を開き、表の行のリンクで発生 30 分前からの関連ダッシュボード・ログ・Alertmanager（silence / inhibit）へ進みます。
- 正常 / Pending / Firing / 抑制中 / 欠測 / 評価失敗 / 監視対象の消失を別々に表示します。
- ダッシュボードは `scripts/alert-entry-dashboard/generate.bb` で生成し、`scripts/alert-entry-dashboard/test.sh` で確かめます。JSON を直接編集しないでください。
- Grafana の `alertmanager` データソースは `handleGrafanaManagedAlerts: false` です。Grafana 管理ルールをこの Alertmanager に流さず、二重通知を作りません。
- 棚卸しの結果は `docs/project_docs/BOXP-623/plan.md`。
