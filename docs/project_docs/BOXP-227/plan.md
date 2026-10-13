# BOXP-227: PR #818 CI修正と認証経路の調査計画

対象は PR #818 の未反映CI修正とGit/GitHub認証経路。親223のESO更新・CRD移行・admission・egress・live試験は再開しない。

1. 通常runnerのsummary/claimとTask Board lane、PR state/head/base、既存worktreeの所有・変更状態を読取確認する。
2. 修正commit、revert、保存patch、現在のworkflow本体を比較し、ローカルcommit・remote履歴・PR最終差分・CI結果を区別する。
3. HTTPS/SSH remote種別、credential helper分類、環境overrideの有無、gh認証scopeを非秘密メタデータだけで確認する。credential store・秘密鍵・token・raw stderrは読まない。
4. 提供された本run専用branchをPR headへ合わせ、patchを実workflowへローカル適用する。古いworktree・PR branchは編集しない。
5. workflow trigger/permissions/secrets参照/後段/自動mergeを確認し、unittest・actionlint・実shellによる版選択/immutable不一致拒否・patch整合性・機密scanを実施する。
6. 承認済みworkflow書込経路がある場合だけ、PR headを再確認して限定commitを通常pushする。認証再発行・scope追加・SSH登録が必要ならpushせずBlockedとする。
7. 結果をObsidian ProjectsへMarkdownで保存し、227と223 Notesへリンクする。PRをmerge/ready化せず、本番設定・runner全体設定・認証課金方式を変更しない。

2026-10-13の判断: 現在のHTTPS pushはgh helperを使用し、ghのscopeは `gist/read:org/repo` で `workflow` がない。承認済み別書込経路は確認できず、ローカル成果の検証まで実施し、remote反映は人の公式認証手続き待ちとする。詳細は [investigation.md](investigation.md)。
