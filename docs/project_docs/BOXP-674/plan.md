# BOXP-674 通知labの条件待ち

## 目的と範囲

通知テストの検証範囲を保ち、結果が成立したところから次へ進める。変更対象は `tests/hermes-agent/notify-lab/stage2.sh`、期限付き待機helper、その回帰テストと資料。本番通知コード、workflowの起動条件・必須check、Kubernetes更新、archのLean標準化は変更しない。

## 実装

単調時計で期限を計算し、残り時間をprobeコマンドの実行上限にも渡す。probe出力を表示せず、成功を確認できないまま期限を迎えたら非ゼロで終了する。既存の失敗伝播・証跡収集・cleanup処理は保持する。

| 場面 | 次へ進む条件 |
|---|---|
| 初回起動・再起動 | 新しいconnect記録とgateway、LLM stub、home stubの応答。上限90秒。再起動前のconnectは流用しない |
| T1/T3/T5/T8 | 対象eventのdelivered記録。上限90秒 |
| 朝digest | digestのdelivered記録。claim graceの8秒は維持 |
| T7片側障害 | 対象eventのdelivered_partialを確認して障害を解除し、recoveredを確認 |
| T9両側障害・再起動 | 対象eventのdelivery_failedとrecovery_failedを確認して再起動。新connect後に障害を解除しrecoveredを確認 |
| Alertmanager | firingのdelivered、同文のduplicate_dropped、resolvedの2件目のdeliveredを順に確認 |
| cron移行 | 指定2 jobのdeliver / failure_deliverが期待値へ移ったこと。上限120秒 |

LLM要求、Notes、HTTP受理だけを配送完了と判定しない。T2の15秒、T4の20秒、quiet切替の8/35秒、digest claim graceの8秒、旧routeの10秒、reminderの5秒、cron再実行の100秒×2は元のまま残す。不在を即時に成功としない。stage0/stage1など別経路は今回変更しない。

`check-stage2.bb` の件数、無通知、重複、quiet/digest、片側/両側障害と再起動、旧route拒否、cron、Alertmanager、画像route、全plugin validationの検査は変更しない。

## Lean TDDと検証

通知プロダクトの仕様・実装を変更しないテストハーネスの待機改善として、新規のLeanモデルは適用しない。既存モデルへ形式的な添付をせず、ハーネス自体の成功・遅延成功・期限切れ・ハング時の打切りを回帰検証する。陰性シナリオは元の観測窓と統合checkerを維持する。

- 子へ起動・移行の条件待ちと回帰検証を委任し、親で再検証・配送終端の待機を統合した。委任helperのverifyは成功。
- 待機回帰3件、Hermes関連25件、既存通知単体、全labシェル構文、diff検査を実行。PyYAMLとキャッシュはrun workspace内に配置。
- 子の未使用陰性APIは再検証で期限直前probeの不安定さが判明したため除去した。fixtureの構文誤りも修正後に再実行して成功。
- codex-reviewは61ef07b4のコード差分に指摘なし。実imageによる統合検証と全体CI時間はGitHub Actionsで前後比較する。

## 計測と完了条件

[baseline.md](baseline.md) に動作不変の比較基点と全workflowを記録する。同一PRイベント、同一image digest、同一arch SHA、同一runner環境を照合する。全CIは最早workflow作成から最遅job完了までとし、job本体と隔離stage2本体も別に集計する。1組の実測を一般的な平均やSLOとは呼ばない。

通常のgitleaks・status-check、統合lab、P0/P1レビューを確認し、最新TODO revisionをack後にmergeする。mainの通常CIと同digestの手動labを確認して、効果と未確認を `Projects/test-ci-inventory/` に納品する。workflowはmain pushでlabを起動しないため、main実行には既存workflow_dispatchを使用する。

## 実imageによる起動条件の修正

実CIでconnectとHTTP待受だけでは不十分と判明した。gatewayはTelegram接続の起動処理中にwebhookを待受し、runtime authority登録前のPOSTはprofile_mismatchで503となる。新しいconnectに加えてgateway_stateがrunningであることを必須とし、/healthの200も確認する。

imageのentrypointはhermesのUIDを起動中に10000から1000へ変更する。初期の名前解決に依存するとlabファイルが異なる所有者で作られるため、stage2の実行UIDをDockerfileの設定と同じ1000:10000へ固定する。権限を緩めず、同じ利用者として準備・実行する。失敗時にもjournal、gatewayログ・状態、dispatcher、labの受信証跡を回収し、元の失敗exit codeを維持する。

変更前mainもT8でroutingファイルへのPermission deniedとなったため、この失敗実行を高速化の基点にはしない。起動条件の修正版と動作する基点を再検証する。
