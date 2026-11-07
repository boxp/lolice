# BOXP-463: Gemini API 前払い残高の Grafana 可視化

## 調査結果（公式資料）

- Gemini API の Prepay 残高は **billing account 単位**。同じ billing account に紐づく全 project の API キーが同じ残高を使う。
  残高 0 で全キーが HTTP 402 になる。課金 pipeline に約 10 分の遅延があり、負残高もありうる。購入から 12 か月で失効し、返金はない。
  （https://aistudio.google.com/docs/billing）
- 残高の閲覧・取引履歴は **AI Studio の Billing タブ**（と Google payments center）だけ。
  残高を返す公式 API・Cloud Monitoring metric・BigQuery export の行は**文書化されていない**。
- Cloud Billing の BigQuery export は Gemini API の利用コストを含むが、配送遅延の保証はない（通常 1 日以内）。
  prepay の差引や残高の行は文書化されていない。有効化には billing account 側の設定と新しい読取権限が要る。
  （https://docs.cloud.google.com/billing/docs/how-to/split-charging-cycle, export-data-bigquery-tables）

したがって「Google 側で確認された実残高」を自動で取る手段は現時点でない。未文書化 endpoint・cookie の抽出・広い権限の追加はしない。

## 実装方針

既存の `codex-workspace` の `ai-ops-metrics` sidecar（read-only、`/home/boxp` を読む）に、
BOXP-462 の subscription 指標とは**別ファイル・別 prefix**（`ai_prepaid_*`）で次を出す。

1. **実残高**: `ai_prepaid_balance_actual_state{state="unsupported"}`。公式取得手段がないことを明示する。値は出さない。
2. **基準残高**: Owner が AI Studio Billing タブで確認した金額・通貨・確認時刻（`baseline.json`）。
3. **推計残額**: 基準残高 −（基準時刻より後に当環境の client が `usage.jsonl` に記録した Gemini 利用費の推計）。
   当環境外の消費・課金遅延・失効・購入は反映されないので「推計」と明示する。
4. **状態**: 基準未設定・基準不正・ledger 未作成/読取不可・ledger 不正・通貨不一致を one-hot の state で区別する。未取得を 0 や十分な残高として扱わない。
5. **鮮度**: 基準の確認時刻からの経過、ledger の最終記録時刻、生成時刻。

入力ファイル（`/home/boxp/.codex-task-board/ai-ops/gemini-prepaid/`、非秘密、本文/明細/キーは書かない）:

- `baseline.json`: `{"schema":"gemini-prepaid-baseline/v1","amount":12.34,"currency":"USD","observed_at":"2026-11-07T18:40:00Z","source":"ai_studio_ui_owner"}`
  購入・付与・失効があったら Owner が AI Studio で再確認した値で置き換える。
- `usage.jsonl`: 1 行 1 呼出し。`{"ts":"...","client":"kohako_studio","model":"gemini-...","estimated_cost":0.13,"currency":"USD","basis":"estimate"}`
  441 の Studio 試験 ledger から記録する。

Grafana dashboard `gemini-prepaid`（`AI / Gemini API 前払い残高`）を新設し、実残高・基準・推計・状態・鮮度・限界の注記を分けて表示する。
既存 `ai-ops-quality` と 462 の forecast rule とは別ファイル。

## 非対象・推奨

- 自動 top-up・追加購入・後払い変更・新たな広域権限・請求明細や credential 本文の収集はしない。
- 低残高と取得失敗の通知は推奨のみ（info alert: 推計残額が初回試験上限 US$5 未満、基準が 7 日以上古い、state≠ok）。今回は配備しない。
- BigQuery billing export による実利用額の照合は、Owner の判断（billing export 有効化と読取 SA）が必要な別案。

## 検証

- server.py の単体試験（基準なし・正常・通貨不一致・不正行・負残高）。
- Chainsaw `codex-workspace-metrics` に prepaid 指標の確認を追加。
- 通常 CI → codex review P0/P1 → merge → Argo 反映 → 実 /metrics・Prometheus・Grafana dashboard の readback。
