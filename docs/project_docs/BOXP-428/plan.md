# BOXP-428 Langfuse self-host と Task Board / Hermes / G2 トレース 計画

正本: Obsidian `Projects/langfuse-observability/request.md`。旧 BOXP-385 の「見送り・metadata-only」は Owner の最新選択で更新済み。

## 構成（最新公式 v4.75.0 / 2026-11-05 確認）

| 要素 | 実装 | 主な上限 |
|---|---|---|
| Web / Worker | `langfuse/langfuse(-worker):4.75.0` (MIT) | 各 limit 1.5Gi |
| PostgreSQL | 16 StatefulSet | PVC 5Gi |
| ClickHouse | 単一 replica (`CLICKHOUSE_CLUSTER_ENABLED=false`)、system log TTL 短縮、低メモリ設定 | limit 4Gi / PVC 20Gi |
| Valkey | 8、`noeviction`、永続化なし (emptyDir) | maxmemory 256Mi |
| S3 互換 | MinIO 単一、event 1日 lifecycle | PVC 10Gi |
| UI 経路 | cloudflared + Cloudflare Access (Owner email + GitHub)。`/api/public/*` は tunnel で 404 | |

- 配置: priority 負値・`preemptionPolicy: Never`、CP と golyat-4（研究GPU）を nodeAffinity で除外。
- Longhorn: 専用 StorageClass（1 replica・`Delete`）、recurring backup group 無し＝backup 対象外。
- 通信: Calico で namespace 既定拒否。ingress は cloudflared / codex-workspace / hermes-agent の :3000 のみ。egress は DNS と namespace 内のみ（Web/Worker 外部 telemetry を実質遮断、`TELEMETRY_ENABLED=false` 併用）。

## Retention（OSS 境界）

Data Retention は EE のため使わない。`langfuse-retention` CronJob が ClickHouse を走査して 14 日超と共有中の trace を公開 API で削除し、bucket lifecycle で raw event を 1 日で失効させ、media upload は無効のまま（S3 media bucket 未設定）とし、ClickHouse の system log は作らない。

事前検証（dind 上の同設定スタック、v4.75.0）で判明した事実:

- 既定の `events_only` モードでは `GET /api/public/traces` が 404。一覧は `/api/public/v2/observations` を使う。
- `DELETE /api/public/traces` は ClickHouse の行を消すが **raw event オブジェクトは消さない**。lifecycle が唯一の失効経路。
- 公開 API の一覧は `public` で絞り込めず、ページ上限を超えると後続の共有 trace を取りこぼす。検出は `events_core` への直接クエリで全件を走査し、1 回の上限を超えた分は削除後の次回実行で続きを処理する。
- media upload は S3 media bucket 未設定で無効。media は同一内容を複数 trace が共有するため作成日基準の lifecycle は掛けない。有効化する場合も、参照が無くなった media は trace 削除時に Langfuse が消す。
- 削除行は part の merge まで残るため、毎日 `OPTIMIZE FINAL` する。

## Producer

- **Task Board**: `docker/codex-workspace/task-board/langfuse_exporter.bb` を codex-workspace の sidecar で実行。runner を変更せず、終了済み run の artifact とセッションログ（Claude / Codex / 委譲子 / review gate）から trace を作る。有効化時刻より前の run は送らない（backfill なし）。
- **Hermes / G2**: Hermes プラグイン `boxp-langfuse-observer`（標準ライブラリのみ、hook は put_nowait のみ）。G2 BFF は Hermes `/v1/runs` 経由のため `kohako-g2-*` session としてこの経路に乗る。音声は扱わない。
- **結合**: Task Board の session.id = 票番号、双方に `ticket:BOXP-N` tag。
- **秘密除外**: パス／コマンド単位の構造的除外 → 正規表現・実環境の秘密値マスク → 送信／spool 直前の全 payload 再走査。ヒットや例外は当該観測を捨て理由だけ残す（fail-closed）。fixture は実行時連結の合成値のみ。
- **失敗時**: 非同期・有界 queue/spool、短いタイムアウト、欠測カウンタ。本業務を止めず再実行もしない。

## 欠測の明示

trace metadata の `coverage.*` と日本語入口に記載: 非公開推論は収集しない / CLI が provider へ送った payload のうちログに残らない部分 / G2 realtime モデル自身の音声会話 / Hermes の proxied 内部処理。

## 順序

1. arch: Cloudflare tunnel/Access terraform (PR) → apply
2. lolice: `argoproj/langfuse` (PR) → 配備・合成 trace・削除 readback
3. arch: exporter (PR) → image
4. lolice: sidecar / Hermes plugin / NetworkPolicy (PR)。merge で codex-workspace 再起動あり
5. 実担当の小さな依頼で trace 確認 → G2 最小本人確認 → 日本語入口・退出手順

## 動的検証（Chainsaw）

Owner 追加要件により `tests/chainsaw/langfuse` を通常 CI に接続する。`argoproj/langfuse` か suite の変更で detect が component を選び、失敗は `status-check` へ伝播する。`.github/workflows` は変更しない（自動の push 権限に `workflow` scope が無いため）。ESO は suite が本番と同じ chart 版で入れる。workflow 自体の変更時に実行する `required_components` への `langfuse` 追加だけは未実施。

| 検査 | 実体 |
|---|---|
| 起動 | 本番 render の全 Deployment / StatefulSet / PriorityClass |
| Secret | 実 ESO の Password generator から生成した合成値 |
| 権限 | producer の ServiceAccount は `langfuse-ingest-key` の get のみ |
| 通信 | Calico で強制。許可/拒否は timeout で判定し、陽性対照を含める |
| 取込 | OTLP → Worker → ClickHouse / MinIO、親子 span、認証なしの拒否、signup 無効 |
| retention | 実 CronJob の spec で Job を起動し、期限切れ・共有 trace の削除と fresh の残存を API と ClickHouse で確認 |

kind に無い依存は置き換えて本番で別途確認する: Cloudflare tunnel/Access、Parameter Store、Longhorn（render 上の reclaim と recurring job 無しは静的検査）。codex-workspace 側の通信許可は sidecar PR で確認する。
