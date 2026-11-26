# BOXP-598 S3 使用タイプ別の日次費用を Grafana で読めるようにする

Owner 選択 B（毎日取得、月 +約 0.3 USD）。取得と保存は boxp/arch#15696（`/lolice/aws-cost/s3_usage`）。本 PR は読む側。

- ESO `aws-cost-snapshot` に `s3_usage.json` ← `/lolice/aws-cost/s3_usage` を追加。
- `server.py` が `aws_cost_s3_usage_available`、`aws_cost_s3_usage_type_day_usd{usage_type,days_ago}`、`aws_cost_s3_usage_type_day_quantity{usage_type,days_ago}` を出す。summary と同じ snapshot の分だけ出し、`Other` の使用量（単位混在）と null は出さない。系列ごとにまとめて出力する。
- 順序: arch#15696 の apply で SSM パラメータが作られてから merge する（無い key を ESO が参照すると既存の Secret 全体が同期失敗するため）。
- 権限・NetworkPolicy・Grafana 閲覧範囲・アラートは変えない。
- Lean なし: 状態機械のない表示変換で、直接テスト（仕様例）で足りるため。
- 戻し方: 本 PR を revert。
