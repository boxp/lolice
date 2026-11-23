# BOXP-674 notify-lab の待機

## 方針

テストハーネスの待機だけを変更し、本番の通知動作は変えない。単調時計による期限付きポーリングで、gateway の connect と loopback stub の準備、cron 移行の完了を確認する。再起動前の connect は再起動後の完了条件として使わない。期限切れは失敗とし、元の証跡取得や cleanup は維持する。

Task Board の tick は非同期処理の受理であり、Notes の記録や LLM の要求も応答・配送完了を意味しない。error500 の上流再試行、重複・無通知の観測、障害中の回復試行、quiet hours と reminder、cron の重複抑止時間は、明確な完了条件がないため固定の観測時間を維持する。特に不在を即時に成功と判定しない。Alertmanager の順序と既存のチェック・証跡も維持する。

## テスト根拠と Lean TDD

本番コードに触れない試験ハーネスの時間制御であるため、プロダクト仕様の新規テストを先行させる Lean TDD の対象ではない。ただし待機自体の振る舞いは回帰対象なので、成功、期限切れ、観測期間全体の負条件および途中違反を検証する。シェル構文と既存単体テストも実行する。Docker 統合はイメージ・arch checkout が利用可能な場合のみ実行し、未実行なら制限を報告する。

## 実施・検証結果

- stage2 の初回起動と再起動後に、connect の新規記録、gateway、LLM stub、home stub の応答を期限90秒で確認。再起動前の connect 数を基準にする。
- stage2 の cron 移行は2 job の deliver / failure_deliver を期限120秒で確認。未完了は失敗する。
- その他の固定待機は上記の観測上の理由で残した。特に stage2 の dispatcher・LLM・配送・再送・回復全体は、上流の完了が判別できないまま短縮していない。stage1、stage0、standalone の待機も今回未変更。
- 待機関数の成功、期限切れ、停止したコマンド、負条件の全期間と途中違反を単体確認。Hermes テスト24件、boxp-notify テスト1件、全 lab シェルの構文確認、git diff --check は成功。Hermes テストは PyYAML を作業領域内の uv キャッシュから利用。
- Docker daemon は利用可能だったが、対象 Hermes image がローカルにないため統合実行は未実施。実 image での起動・移行条件と統合カバレッジの確認は残る。

## 統合時の追加

親で配送の終端journalを確認し、T1/T3/T5/T8のdelivered、T7のdelivered_partial→recovered、T9のdelivery_failedとrecovery_failed→再起動→新connect→recovered、朝digestとAlertmanagerの完了を期限付きで待つよう追加した。LLM要求やNotesは完了条件に使わない。T2の15秒、T4の20秒、quietの8/35秒とdigest claim graceの8秒、旧routeの10秒、reminderの5秒、cron再実行の100秒×2は元のまま残す。最終check-stage2.bbの全検査は変更しない。

陰性監視用の汎用APIは使用しないため削除。陰性検証を即時判定にしないことはstage2の実シナリオと既存checkerで検証する。待機関数は遅延した成功、減少するコマンド実行予算、期限到達後に再試行しないこと、およびハングしたコマンドの打切りを検証する。

初期の子による陰性APIの実時間テストは親で再実行時に失敗（期限直前のprobe開始が残り予算を使い切る）。未使用の陰性APIを除去し、期限処理の決定的テストへ変更した。
