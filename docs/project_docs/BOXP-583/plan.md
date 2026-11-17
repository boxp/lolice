# BOXP-583: Langfuse trace 保持を 14 日から 30 日へ延長

## 承認と範囲

- 家経由 Owner が「とりあえず30日」を明示承認（2026-11-17）。
- 変更: `langfuse-retention` CronJob の `RETENTION_DAYS` を 14 → 30。`RAW_EVENT_DAYS=1`（MinIO raw event の lifecycle）は維持。
- `files/retention.mjs` の既定値も 30 に揃える（env 欠落時に旧 14 日で削除しないため）。
- 維持するもの: 公開共有 trace の即削除、秘密除外、本人限定 self-host、監査（件数のみ）、resource 制約、毎時実行。
- 含めないもの: 保持無期限化、raw 延長、backup 方針変更、既に削除済みデータの復元、新権限・課金、本番合成データ追加。

## 変更前の実測（2026-11-17 05:17 UTC の retention 監査、Loki）

- `retention_days=14` / `raw_event_days=1` / `lifecycle=["events/:1d"]`、`expired_traces_deleted=0`。
- ClickHouse: 2,071 trace、最古 trace の年齢 278.4 時間 → 最古 `start_time` は 2026-11-05 14:53 UTC 頃。
  - 14 日のままなら 2026-11-19 14:53 UTC 頃から実削除が始まる。30 日では 2026-12-05 14:53 UTC 頃に初めて対象が出る。
- PVC 使用量（票記載の参考値）: ClickHouse 約 123MiB / 19.5GiB、MinIO 約 58MiB / 9.75GiB、Postgres 約 70MiB / 4.84GiB。CH 直近 7 日の純増 約 66MiB。

## 容量と監査負荷の見積り

- ClickHouse: 30 日分は 7 日純増 66MiB × 30/7 ≈ 283MiB 程度（現行の倍率で増えても 1GiB 未満）。19.5GiB に対し十分小さい。
- MinIO: raw event は 1 日のまま。保持延長の影響なし。Postgres も trace 本体を持たないため影響は小さい。
- 監査: `audit()` は `events_core` / `events_full` を FINAL で全走査する。行数は最大で約 2 倍強になるが、現状 Job 全体 13 秒・`max_execution_time=60`、`activeDeadlineSeconds=900` に対し余裕がある。配備後の Job 所要時間で確認する。

## 確認手順

1. Chainsaw の retention テストを 30 日境界へ更新（31 日前は削除、15 日前は残存）。PR の CI（argocd-diff 等）と codex review を通し merge。
2. Argo CD の `langfuse` Application が Synced/Healthy、実 CronJob の env が `RETENTION_DAYS=30` / `RAW_EVENT_DAYS=1`。
3. 配備後に作成された新 Job が Complete、Loki 監査で `retention_days=30` / `raw_event_days=1` / `lifecycle=["events/:1d"]`、Job 所要時間。
4. 正本 `Projects/langfuse-observability/owner-summary.md` の保持説明を 30 日へ更新。
5. BOXP-461 を「30 日超の実削除確認（2026-12-05 15:00 UTC 以降）」へ正規 helper で更新し、将来確認の予約 ID を残す。

## 区別

- 本 PR で確認できるのは「30 日設定の配備」まで。30 日超 trace の本番実削除は 2026-12-05 頃まで対象がなく、未確認（BOXP-461 で確認）。
