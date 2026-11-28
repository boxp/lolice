# BOXP-769: lolice の障害対応フローと DORA 復旧時間の記録

仕様（現行）: Obsidian `Projects/lolice-incident-recovery/spec.md`、手順: `Incidents/Runbooks/incident-response-dora.md`。

## 棚卸し

- 障害記録: `Incidents/Tickets/INC-1..5`（全件インフラ障害）。frontmatter に started/detected/mitigated/resolved はあるが、原因の配備・復旧操作・利用者影響の解消の確認と根拠の欄がない。
- DORA 収集器（BOXP-762, `lolice-dora-metrics`）: Argo CD 履歴と公開 git だけを読み、vault は読めない（CD-01 の権限制約）。復旧時間は手当て配備どうしの関連づけから出す。
- vault を読む既存経路: codex-workspace の ai-ops-metrics サイドカー（`/home/boxp` 読み取り専用、BOXP-570 で Tickets を解析、prometheus-k8s が収集）。
- DORA 公式（2026-01-05）は失敗配備からの復旧時間の起点・終点を定めず、配備によらない障害を含まない。

## 方針

1. 記録: INC frontmatter に `schema: incident/v1` の欄（影響開始・検知と根拠・原因種別と原因の配備・復旧操作・利用者影響の解消の確認と根拠・dedupe_key・duplicate_of・synthetic）。v1 より前は legacy（集計外・書き換えない）。
2. 書き込み: `scripts/incident/incident.bb`（open/cause/recover/confirm/correct/duplicate）。時刻は既定で今、原因の配備は Argo CD 履歴から引く、根拠なしは拒否、同じ dedupe_key の未解消記録があれば open を拒否、訂正は Notes に旧→新。
3. 収集: ai-ops-metrics サイドカーが `INCIDENTS_DIR` を読み `lolice_incident_*` を出す（ID と列挙だけ）。DORA = 原因配備 deployedAt → 解消確認、一般 = 影響開始 → 解消確認。
4. 表示: Grafana `lolice-dora` に「障害記録と復旧時間」の行。合成事例は変数で切り替え。
5. 検証: 合成事例（`Incidents/Synthetic/SYN-N.md`）で 検知→記録→復旧確認→表示。

## やらないこと

本番障害の注入、既存通報・アラートの抑制や変更、新しい権限・課金・外部送信、INC-1..5 の書き換え、収集器の変更失敗率への自動合算。

## 注意

server.py・deployment.yaml の変更は codex-workspace Pod の再作成（Recreate）を伴う。
