# BOXP-227: PR #818 CI修正と認証経路の調査計画

対象は PR #818 の未反映CI修正とGit/GitHub認証経路。親223のESO更新・CRD移行・admission・egress・live試験は再開しない。

1. 通常runnerのsummary/claimとTask Board lane、PR state/head/base、既存worktreeの所有・変更状態を読取確認する。
2. 修正commit、revert、保存patch、現在のworkflow本体を比較し、ローカルcommit・remote履歴・PR最終差分・CI結果を区別する。
3. HTTPS/SSH remote種別、credential helper分類、環境overrideの有無、gh認証scopeを非秘密メタデータだけで確認する。credential store・秘密鍵・token・raw stderrは読まない。
4. 提供された本run専用branchをPR headへ合わせ、patchを実workflowへローカル適用する。古いworktree・PR branchは編集しない。
5. workflow trigger/permissions/secrets参照/後段/自動mergeを確認し、unittest・actionlint・実shellによる版選択/immutable不一致拒否・patch整合性・機密scanを実施する。
6. 承認済みworkflow書込経路がある場合だけ、PR headを再確認して限定commitを通常pushする。認証再発行・scope追加・SSH登録が必要ならpushせずBlockedとする。
7. 結果をObsidian ProjectsへMarkdownで保存し、227と223 Notesへリンクする。PRをmerge/ready化せず、本番設定・runner全体設定・認証課金方式を変更しない。

2026-10-13 前runの判断: HTTPS OAuthにworkflow scopeがなく、ローカル検証までで停止した。

2026-10-13 再開runの判断: ownerによる既存SSH経路の利用承認を確認した。標準の既存鍵とknown_hostsを用いてBatchMode/StrictHostKeyChecking=yesでGitHubのboxp認証に成功した。PR headは不変。前run commitを本run専用branchへ再利用し、再検証後に明示SSH宛の通常pushで既存PRを修復する。HTTPS origin・gh API認証・runner設定を変更しない。push前後にremote head/workflow blobと223 worktree/claimを再確認し、対応CI結果を記録する。詳細は [investigation.md](investigation.md)。


2026-10-13 レビュー再試行計画: PR head104bedfへの実workflow反映とCI成功を確認済み。
適用済みpatchを削除し、223のplan/validationと227の調査記録を現在状態に訂正する。
指定run worktreeだけを編集し、diff/機密scan/独立レビュー後に同じPRへ限定文書commitを通常pushする。
workflow本体は変更せず、新headのCI・mergeabilityを確認する。
