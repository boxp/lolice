# BOXP-594: AWS コスト共通参照（OIDC 日次取得 → SSM → ESO → Prometheus → Grafana）

設計の正本: Obsidian `Projects/aws-ai-agent-cost-readonly-role/research/shared-cost-reference-design.md`（BOXP-593、案 B）。
定義の版: `aws-cost/v1-draft`。

## 原則（レビュー項目）

- コハコ・runner・クラスタ内 Pod に AWS 資格を渡さない。新しい長期キーを作らない。
- 取得は GitHub Actions の OIDC 短期認証だけ。信頼条件は `repo:boxp/arch:ref:refs/heads/main` と `aud=sts.amazonaws.com`（environment は使わない。sub が変わるため）。
- 権限は `ce:GetCostAndUsage` `ce:GetCostForecast`（`*`）と、`/lolice/aws-cost/*` に限った `ssm:PutParameter` `ssm:GetParameter`（GetParameter は回数ガード用に自分の status を読むためだけ）。支払・税務・請求書・S3・Organizations・CUR/Data Exports・時間粒度は付けない。
- 公開 repo / Actions ログ / artifact / PR に請求値を出さない。ログに出すのは件数・成否・AWS のエラーコード名だけ。artifact は作らない。`set -x` を使わない。
- 失敗・欠測を 0 と読ませない。失敗時は前回 snapshot を上書きしない。
- Grafana の閲覧範囲を変えない（dashboard は追加しない。anonymous 無効・login form のみであることを確認済み）。

## arch

### `terraform/aws/aws-cost-snapshot/`（tfaction の新 working directory。`even-g2-lab` と同じ構成）

- `backend.tf`（key `terraform/aws/aws-cost-snapshot/v1/terraform.tfstate`）、`provider.tf`、`variables.tf`（`aws_account_id`）、`tfaction.yaml`、`aqua/`。
- `iam.tf`: role `aws-cost-snapshot-gha-role` と上記の最小 policy。
- `ssm.tf`: SecureString 4 つ（`/lolice/aws-cost/summary` `services` `daily` `status`）。初期値は `{"schema":"aws-cost/v1-draft","state":"uninitialized"}`、`lifecycle { ignore_changes = [value] }`。SecureString なので plan に値が出ない。

### `scripts/aws-cost-snapshot/snapshot.bb`

1 回の流れ（UTC 日付）:

1. `sts get-caller-identity`（権限不要）で account 末尾 4 桁。
2. `/lolice/aws-cost/status` を読み、ガード判定。直近 20h 以内に成功があれば skip。直近 24h の試行が 3 回以上なら skip。skip は成功終了（値は書かない）。
3. 試行を status に記録してから Cost Explorer を呼ぶ。
4. `GetCostAndUsage`: 期間 = 前月 1 日〜今日（終端は排他）、DAILY、`UnblendedCost`、GroupBy `SERVICE` と `LINKED_ACCOUNT`。NextPageToken を最大 5 ページ。超えたら失敗（`PageLimitExceeded`）、何も上書きしない。
5. `GetCostForecast`: 今日〜翌月 1 日、MONTHLY、`UNBLENDED_COST`、`PredictionIntervalLevel=80`。失敗（履歴不足の `DataUnavailableException` など）は致命扱いせず「予測なし」。
6. 1 回の上限は合計 6 リクエスト。
7. 変換して `daily` → `services` → `summary` の順に PutParameter（`--overwrite`、SecureString）。各 4000 バイト超なら失敗。最後に status を ok に更新。
8. 失敗時は status に `last_result=error` と AWS エラーコード名（英字のみに無害化）を記録して非 0 終了。

snapshot の中身（JSON、`snapshot_id` = 取得時刻 epoch 秒、全部に共通）:

- summary: `schema` `snapshot_id` `fetched_at` `data_through`（今日-1日）`account_suffix` `linked_accounts`（LINKED_ACCOUNT の種類数。>1 なら組織の管理アカウントで他アカウント分も含む）`mtd_usd` `prev_month_total_usd` `prev_month_same_period_usd`（前月 1 日〜同じ日数、前月の日数で頭打ち）`estimated_current` `estimated_prev`（期間内に `Estimated=true` の日が 1 つでもあれば true、日が無ければ null）`forecast`（`available` と、今月累計＋残り予測の `mean` `lower` `upper`）`requests` `pages`。
- services: 今月累計のサービス別、上位 10 + `Other`。
- daily: 直近 14 日（`days` 昇順）× 直近 14 日合計の上位 10 サービス + `Other` の行列。
- status: `last_attempt_at` `last_success_at` `last_result`（ok/error/skipped）`error_code` `attempts`（24h 以内の試行 epoch）`requests_last`。

### `.github/workflows/aws-cost-snapshot.yml`

- `schedule: "0 23 * * *"`（08:00 JST）と `workflow_dispatch`。pull_request では AWS を呼ばない。
- `permissions: id-token: write, contents: read`、`concurrency` で多重実行なし、`timeout-minutes: 10`、ubuntu-latest。
- bb を既存 workflow と同じ方法で入れ、`aws-actions/configure-aws-credentials`（既存と同じ SHA pin）で role を引き受ける。

### テスト

- `tests/aws-cost-snapshot/`: 偽の Cost Explorer 応答（架空の金額）で変換・日付計算・上位集約・ページ上限・予測なし・月初・前月日数頭打ちを確認する bb テスト。
- `.github/workflows/test-aws-cost-snapshot.yml`: pull_request でテストだけ実行（AWS 認証なし）。

## lolice（`argoproj/prometheus-operator`、namespace `monitoring`）

- `ExternalSecret aws-cost-snapshot`: ClusterSecretStore `parameterstore` から 4 param を `summary.json` `services.json` `daily.json` `status.json` に。refresh 1h。
- `Deployment aws-cost-metrics`: `python:3.15-alpine`（ai-ops-metrics と同じ）、AWS 資格なし、Secret を読み取り専用で mount（subPath なし）、`server.py` は configMapGenerator。port 9465、`/metrics` `/healthz`。非 root・読み取り専用 rootfs。
- `Service` / `ServiceMonitor`（`app.kubernetes.io/part-of: kube-prometheus`）/ `NetworkPolicy`（ingress は prometheus のみ、egress なし）。
- metric（`aws_cost_` 接頭辞）: `snapshot_available` `snapshot_info{definition,account_suffix}` `snapshot_consistent` `fetched_timestamp_seconds` `data_through_timestamp_seconds` `mtd_usd` `mtd_by_service_usd{service}` `day_usd{service,days_ago}`（取得日基準 1〜14）`prev_month_total_usd` `prev_month_same_period_usd` `forecast_available` `forecast_month_end_usd{bound}` `estimated{period}` `fetch_requests` `linked_accounts` `last_attempt_timestamp_seconds` `last_attempt_success` `last_attempt_error_info{code}`。未初期化・壊れた JSON は `snapshot_available 0` だけを出し、金額 metric は出さない。
- `PrometheusRule aws-cost-rules.yaml`（severity info、メール・Hermes 転送なし）: `AwsCostSnapshotStale`（取得から 36h 超、未取得、配信 Pod 不在）と `AwsCostFetchFailed`（直近の試行が失敗）。promtool テストを追加。

## 利用側の読み方（コハコ・runner 共通）

既存の Grafana datasource proxy（Prometheus uid `P1809F7CD0C75ACF3`）で同じ metric を読む。`time() - aws_cost_fetched_timestamp_seconds > 36h` または `aws_cost_snapshot_available == 0` は「不明」。金額系の欠測を 0 と読まない。今月分は `aws_cost_estimated{period="current"} == 1`（見込）。

## 費用

通常 1 日 2 リクエスト（約 0.6 USD/月）。1 回 6 リクエスト上限、24h で 3 回までなので、失敗が続いた最悪でも 1 日 18 リクエスト（0.18 USD/日）。SSM standard は無料。

## 配備と確認

1. arch PR merge → tfaction apply で role と空の param を作成。
2. lolice PR merge → Argo CD で配信 Pod。初期値なので `aws_cost_snapshot_available 0`。
3. `gh workflow run aws-cost-snapshot.yml` で初回取得。ログは件数と成否だけ。
4. runner の Grafana proxy と、コハコの Grafana MCP が使う同じ datasource で `aws_cost_*` を読み、同じ値であることを確認。
5. Cost Explorer が未有効なら初回は `DataUnavailableException` 等で失敗し、status に記録される。その場合は Owner にコンソールで Cost Explorer を一度開いてもらう（唯一のコンソール操作）。
