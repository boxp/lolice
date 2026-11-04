# BOXP-405: AI Ops 再設計 段階 2（lolice）— 成果 burn rate の rule と主画面最上段

設計の正本は Obsidian `Projects/ai-operations-observability/redesign.md`（第 2 章: 成果指標 `outcome/v1-draft`、第 4 章: 暫定 SLO と burn rate、第 5 章: エージェントの受領経路）。ここには lolice 側の範囲と検証だけを置く。閾値はすべて根拠付きの暫定で、正式 SLO の宣言ではない。

## 範囲（lolice、段階 2）

1. **`argoproj/prometheus-operator/ai-ops-outcome-rules.yaml`**（新規、PrometheusRule）
   - recording: `ai_ops:outcome_burn_rate{window}` = (not_reached / determined) / 0.10、`ai_ops:outcome_reach_ratio{window}` = reached_in_budget / determined。determined = 0 のときは系列を出さない。
   - alert `AiOpsOutcomeBurnFast`（7d、burn ≥ 2.0 かつ determined ≥ `ai_ops_outcome_min_observations{window="7d"}`、for 2h）、`AiOpsOutcomeBurnSlow`（28d、burn ≥ 1.0 かつ determined ≥ min_observations、for 2h）。どちらも `ai_ops_outcome_export_ok == 1` かつ生成時刻が 2 時間以内のときだけ評価し、計測不調と同時発火しない。
   - alert `AiOpsOutcomeMeasurementStale`（export_ok == 0、生成時刻が 2 時間超、または系列欠落、for 30m）。成果の悪化とは別名で通知する。
   - 共通 label `component=ai-ops` / `severity=warning` / `definition_version=outcome/v1-draft`。annotation に許可された次 action と禁止操作の固定文。
   - 観測不足（最低観測数未満）はアラートにせず画面表示のみ。Alertmanager の受信先・新しい通知先・secret は追加しない。
2. **promtool unit test** `tests/ai-ops-outcome-rules.test.yaml` と `tests/run-promtool-tests.sh`（PrometheusRule の spec を抽出して promtool に渡す。ホストの promtool または `prom/prometheus:v3.9.1` の docker）。case: 7d 劣化あり / 劣化なし / 観測不足で発火しない / 28d 継続悪化 / export_ok=0 で計測不調のみ / 系列欠落 / 生成時刻が古い / 重複なし / recording rule の値。CI `promtool-rules-test.yaml`（pull_request、contents: read）。
3. **Grafana `ai-ops-quality`** の最上段に row「0. 成果の状態・ハーネス劣化の疑い・判断可能性・次 action」と 4 panel を追加。既存 panel は y をずらすだけで式・文言は変えない。対照評価は段階 3 まで「未実施」と固定表示。No data は「未計測」と「発火なし」を description で区別する。

## 範囲外

Alertmanager 設定・受信先の変更、`AiOpsHarnessEvalRegression`（段階 3 で `ai_ops_harness_eval_*` を導入後）、SLO 正式値の承認、arch 側の export（PR boxp/arch#14461）と runner の受領経路（arch 側 PR）。

## 配備の注意

PrometheusRule と dashboard ConfigMap は ArgoCD の同期だけで反映され、Pod 再起動を伴わない。`ai_ops_outcome_*` 系列は arch 側の配備（codex-workspace Pod の再起動を伴う。時期は Owner 確認）まで存在しないため、それまでは `AiOpsOutcomeMeasurementStale` が「系列欠落」で発火する。これは計測未開始の正しい表示であり、成果の悪化ではない。

## 検証

```sh
argoproj/prometheus-operator/tests/run-promtool-tests.sh   # SUCCESS
python3 -I -c 'import json; json.load(open("argoproj/prometheus-operator/dashboards/ai-ops-quality.json"))'
```

## 成果の定義の改訂（`outcome/v2-draft`）と対照評価のアラート

実データの readback で分かった欠陥を直す。集計側の変更は arch #14465 で、rule と同時に反映する。

- burn rate を `(determined - reached_in_budget) / determined / 0.10` にする。以前は遅れて届いた作業を失敗に数えず、目標の「予算内の到達率」と合っていなかった。
- `AiOpsHarnessEvalRegression` を追加する。`ai_ops_harness_eval_regression{status="suspected"}` が 1 で export が正常なときに発火する。
- 主画面の「ハーネス劣化の疑い」を固定文から実測の表示に変える。未実施と劣化なしを区別する。
