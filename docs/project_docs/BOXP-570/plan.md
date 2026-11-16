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

## 再設計（Owner 不合格を受けて、Opus が決定）

Owner 指摘: bin・pt・used%・ticket_label・*_tokens_sum・Mil などの略語/英語が混在し読めない。上段の実測（全体）と推定（票選択に連動）が同じ並びで区別できない。

目的を「票ごとに、どのモデル・経路がどの週枠をどれだけ減らしたか（推定）を読み、担当モデルを決める」に固定し、widget を次の順に作り直す。

| 位置 | widget | 役割 | 選択に連動 |
|---|---|---|---|
| 1 | この画面の読み方 | 読む順番・実測と推定・週枠の種類・選択の効き方 | ― |
| 上段 | 票ごとの枠負担 | 枠を多く減らした票を探す（共有週枠の推定で降順） | する |
| 上段 | モデル × 経路ごとの枠負担 | 票の中で親/子委譲/レビューのどれが重いか | する |
| 上段 | モデル別の枠負担 | モデル間の重さの比較、1 回あたりの大きさ | する |
| 上段 | 推定した週枠消費の推移 | いつ減ったか（集計間隔ごと、積み上げなし） | する |
| 上段 | 記録の欠けと推定に入らない量 | 推定が小さくなる原因の量 | する |
| 下段 | 週枠の使用率（実測） | アカウント全体の実測 | しない |
| 下段 | 推定の合計と実測の比較 | 実測増加・推定合計・説明できない分・票あり/票なし | しない |
| 末尾 | 推定の根拠と限界 | 係数表（日本時間の期間）・過小になる理由 | ― |

- 単位は「週枠に対する %」と「トークン（百万）」に統一。推定は中央値と推定幅（下限〜上限）を別列で出す。
- 変数名: 票 / モデル / 経路 / 提供元 / 集計間隔（1時間・6時間・1日）。Prometheus 選択は隠す。
- 測定済み 7 日間の照合は `ticket-usage-live-check.sh`（推定合計 = 票あり + 票なし）で行い、画面からは内部検算列を外す。
- 検証: 生成テスト（略語禁止・下段が選択に連動しないこと）、CI、merge 後の配備、実ブラウザ描画を Opus が目視。

### 実描画で直したもの（2 回目、PR #1259 配備後の描画で発見）

- 「推定した週枠消費の推移」: 週枠ごとの棒が同じ位置に重なり、Claude 共有週枠が Codex の棒に隠れて読めなかった。積み上げると独立した 3 つの週枠を足すことになるため、線 + 点に変更する。
- 「推定の合計と実測の比較」: 半幅では右の 2 列（票に割り当てた分・票なしの分）が画面外に出て、列見出しも切れていた。全幅にする。
- 「記録の欠けと推定に入らない量」: 列見出しが 3 行に折り返して上が切れていた。見出しを短くする（トークン数のない呼び出し / 経路不明の呼び出し / 種類不明のトークン / 最も古い記録）。
