# BOXP-462: AI 利用枠の reset 前枯渇アラートと Hermes 受信

正本: Obsidian `Projects/ai-subscription-burn-alerts/request.md`

## 初期報告（2026-11-08 02:31 JST 前後、read-only）

- Codex primary 7d: used 74%、reset 2026-11-11 16:21 JST。6h・24h のどちらの速度でも reset 前に尽きるペース。
- codex-workspace の `~/.codex/sessions`（token_count）を経路別に集計（本文は分類マーカーだけを参照）。直近 24h の Codex token 約 286M のうち、BOXP-441（コハコ Web モデル）の委譲 Codex 子が約 78%、runner の codex-review gate が約 9%（BOXP-428 の arch#14503 は 7h で 27 head・27 review）、Task Board run 内の Codex 子が約 11%。coordinator の Langfuse 全件集計（Hermes 経路を含む）とも整合（441 子 211.6M、428 review gate 18.1M）。
- token 量・call 数は quota% の内訳でも USD でもなく、比例配賦しない。画像生成の枠が共有か別かは未確認。
- Owner 判断（TD-01）により、追加の token 削減調査・441/428 の反復最適化は当面見送り、アラートと Hermes 受信の整備を続ける。

## 実装

1. `argoproj/prometheus-operator/ai-subscription-forecast-rules.yaml`
   - 記録: 最新観測の used/reset/observed（pod を集約で落とし、観測時刻が新しい系列を採る）、同じ reset 窓内の 6h/24h 増分、速度、burn ratio = v × T / (R − 5)、短期速度での枯渇までの秒数。
   - `AiSubscriptionWindowExhaustionForecast`（warning）、`AiSubscriptionWindowExhaustionImminent`（critical: fast_exhaustion / exhausted）、`AiSubscriptionWindowForecastUnavailable`（info: stale / not_provided / unsupported_window / source_unavailable）。
   - 既存の `AiProviderQuotaExhausted`（いま rejected）や成果 SLO burn とは別。
2. Alertmanager（`external-secret-alertmanager.yaml`）
   - receiver `Hermes`（webhook、`X-Gitlab-Token`、`send_resolved`、`max_alerts: 10`）。route は `notify_hermes="true"` だけ、`continue: true` で既存のメール経路を維持。
   - inhibit: 同じ枠（provider/name/limit）の Imminent が Forecast を止める。
   - token は SecretStore `hermes-webhook-token`（ESO Kubernetes provider、1 Secret だけ読める Role）。
3. Hermes（`argoproj/hermes-agent`）
   - webhook route `alertmanager`: deliver_only（LLM なし）、script `alertmanager_relay.sh` → `alertmanager_relay.clj` で検証・無害化・整形して Telegram へ。mirror_to_session。
   - route 専用 token を Password generator で生成し、init container が PVC 上の route にだけ書く（無い場合は route を無効で登録）。
   - Calico: Alertmanager の Pod から 8644 だけ許可。
4. 一般アラートの転送は切り替えない（opt-in label のみ）。推奨範囲は Notes で 1 つの判断にまとめる。

## 検証

- promtool: `argoproj/prometheus-operator/tests/run-promtool-tests.sh`（持続・速い消費・reset 前後・stale・pod 重複・上限到達・未提供/未対応窓・観測元なし）。
- amtool check-config / routes test。
- Chainsaw: `tests/chainsaw/hermes-agent/alertmanager-relay`（本番の Alertmanager 設定テンプレートと relay script を使い、token 付きで notify_hermes のアラートだけが届くこと、整形結果、不正 payload の破棄）。
- 配備後: 実ルール読込・記録値・発火、ESO 同期、Alertmanager の receiver 設定、Hermes route 登録、明示テストアラートでの受領・解除・dedup、通知失敗時のメール経路、再起動後の復帰を個別に readback。
