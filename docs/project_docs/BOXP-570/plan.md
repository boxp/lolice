# BOXP-570 票別・実モデル別の使用量と推定枠消費 dashboard 計画

依頼: Grafana で ticket ごとの枠消費量を確認したい（BOXP-567 調査の続き、Owner が案 A と同時に依頼）。

## 使うデータ（新しい収集はしない）

| 源 | 内容 | 区分 |
|---|---|---|
| Langfuse ClickHouse view `ai_ops_usage_v1`（BOXP-472） | GENERATION 単位の ticket / 実 model / 経路（親・子委譲・review・subagent・Hermes）/ input・cache read・cache write・output token | **実測**（保持 14 日） |
| Prometheus `ai_subscription_window_used_percent`（BOXP-462/472） | codex primary、claude shared_7d、claude fable_7d の used% | **実測**（アカウント全体、1%刻み） |
| BOXP-567 の 6h 回帰係数（pt / M token） | モデルごとに 1 token が各窓をどれだけ減らすか | **推定**（対象期間 2026-11-09 07:00Z〜11-16 07:00Z、下限〜上限） |

- 高 cardinality の ticket / run は Prometheus label にしない。Langfuse（期間制限付き SQL）で扱う。
- Hermes（ticket が空）は票に配らず「(票なし)」1 区分にまとめる。複数票へ重複計上しない。

## 方式

- 新 dashboard uid `ai-ops-ticket-usage` を `scripts/ai-ops-dashboard/ticket-usage.bb` で生成する（`ai-ops-quality` の panel は変えない）。係数は `scripts/ai-ops-dashboard/quota-coefficients.edn` を正本にし、SQL と説明文の表の両方へ同じ値を埋め込む。
- 変数: ticket（複数選択・All、`(票なし)` を含む）/ provider / model / route / bin。時間範囲は dashboard 共通（JST）。
- 推定 pt は token × 係数の下限・中央・上限で示し「quota 実測」と書かない。係数のないモデルの token は「係数なし」として別に出す。係数の対象期間外の token 割合も出す。
- 照合: 同じ選択期間で、Prometheus の実増分（reset 区間除外、472 と同じ式）と、推定 pt 合計＝票あり＋票なし の内訳、bin 合計を並べる（差分 0 を確認）。
- 欠測: usage 未取得 GENERATION 数、未分類経路、other_tokens、データ最古時刻（保持 14 日）、5 時間窓は未観測、Langfuse の Claude 親が local より少ない既知の欠落（567: 約26%）を説明に明記。

## 範囲外

ClickHouse 権限変更、Prometheus label 追加、保持期間延長、新課金、担当自動変更、会話本文の取得。

## 検証

`scripts/ai-ops-dashboard/test.sh` と新 test（生成の冪等性・係数表の一致・SQL 安全性）、CI、merge 後の GitOps 配備、Grafana API での実 query と実ブラウザ画面で票選択・期間変更・照合差分 0 を確認する。スクリーンショットは vault に置かない。
