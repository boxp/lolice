# BOXP-472 AI Ops 各指標の時系列化と時間帯別 breakdown 計画

正本: Obsidian `Projects/ai-operations-observability/time-series-breakdown.md`。

## 確認した現状

- Grafana uid `ai-ops-quality`（lolice `argoproj/prometheus-operator/dashboards/ai-ops-quality.json`、provisioning）。row 含む 35 + 折りたたみ内 41 panel。ほぼ stat/table の現在値。
- Prometheus retention 31d、ai-ops scrape 60s。系列は `pod` label で分かれ Pod 更新ごとに別系列になる。
- `ai_subscription_window_used_percent` は 1% 刻みの gauge。`ai_subscription:*:latest` recording rule は 2026-11-07 頃から（7d 表示には生系列が必要）。
- token/call の実測は Langfuse のみ（self-host、retention 14d、ClickHouse `events_core`）。Prometheus に token 系列は無い。

## 方針

### 1. 現在値の概要は維持し、全 panel に時系列の入口を追加

- 既存 panel の定義は変えず、`dashboards/ai-ops-quality.json` に row を追加する。生成は `scripts/ai-ops-dashboard/` の Babashka script で再現可能にする。
- 全 panel を `panel-map` に分類する: 時系列 / 状態履歴 / 固定情報 / 未対応（理由と次の最小対応）。未分類の panel があれば test を失敗させる。
- dashboard timezone を Asia/Tokyo に固定する。

### 2. 消費タイムライン → 同期間 breakdown → 詳細

既存の現在値概要（row 0〜3）の直後に row を追加し、利用枠 used%・bin ごとの増加 pt・token 時系列を並べる。同じ dashboard の時間範囲を共有するので、drag/zoom と 6h/24h/7d/custom がそのまま breakdown に効く。

- breakdown は provider / model / 経路 / bin 別 / ticket 別上位 / trace 別上位（Langfuse へリンク）。
- bin は `$__interval`（最小 `$bin` 変数、5m/15m/1h）で、Prometheus と ClickHouse が同じ幅になる。

### 3. 意味を誤らない集計

| 対象 | 扱い |
|---|---|
| subscription used% | gauge。`rate/increase` を使わない。1 分差分を同じ reset 窓の中でだけ合計し、reset 時刻が 1h 超変わった区間は除く。低下（補正）は打ち消して net にする。観測時刻は別系列 |
| token | GENERATION の `usage_details` だけ。親 AGENT/SPAN・trace は足さない。時刻は call の start_time |
| cache | 既定は新規（input + cache write + output + reasoning）。cache read は別系列 |
| 累積 p50/p90・snapshot | 「その時点の全履歴の値の推移」と題に明記。選択期間内の分位ではない |
| counter | `increase`。bin は重ならない |
| token と quota% | 比例配賦しない。並べて表示するのみ |

Task Board は run 終了後に export されるため、実行中の分は後から埋まる。未帰属は `(未分類)` / `(不明)` で表示する。Hermes の複数 ticket 言及は按分しない。

### 4. データ源と境界

- `grafana-clickhouse-datasource` を追加し、Langfuse ClickHouse の HTTP を Grafana から読む。新しい DB・有料サービスは増やさない。
- Grafana 用 user は `default.ai_ops_usage_v1`（SQL SECURITY DEFINER）だけ SELECT 可。本文列 `input`/`output`/`metadata` は view に含めない。readonly profile と実行時間・結果行数の上限を付ける。
- password は ESO generator で cluster 内生成。`monitoring` からは ESO の kubernetes provider で 1 Secret のみ get 可。
- NetworkPolicy は grafana → `langfuse-clickhouse:8123` のみ追加。

### 5. 含めないもの

新しい有料サービス/DB、保存期間延長、既存アラートの閾値変更や停止、過去本文の backfill。Gemini 実残高。token の API 料金換算を実請求として表示すること。

## 検証

- promtool `promql_expr_test`: reset 境界、Pod 切替、欠測、補正、bin 合計一致。
- 生成 script の test: 全 panel 分類、JSON 妥当性、view の列が本文を含まないこと。
- Chainsaw `tests/chainsaw/langfuse`: readonly user が view を読めて `events_core`/本文を読めないこと、NetworkPolicy。
- 配備後: 実 query（Prometheus / `/api/ds/query`）と本人限定 Grafana の実画面で 6h/24h/7d/custom と breakdown の連動を確認する。

## 戻し方

dashboard は JSON の revert で戻る。Grafana の env/plugin、ClickHouse user、NetworkPolicy も同じ PR の revert で戻せる。
