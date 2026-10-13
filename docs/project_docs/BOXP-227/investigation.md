# BOXP-227: workflow push拒否と未反映CI修正の調査

確認日: 2026-10-13 UTC。対象: [boxp/lolice PR #818](https://github.com/boxp/lolice/pull/818)。

## 結論と歴史記録

commit失敗ではなく、workflow書込権限によるpush拒否と、revert後のPR差分欠落を区別する。workflow変更commit `93ec140880d87e9f87d6ce9743272cff3412279e` は作成済みで、revert `1801ad36c01a09ea7a2dbd1b7a653f6b6cc4d66f` とともにremote履歴に存在する。修復前headではworkflow本体が元に戻り、保存patchだけが残っていた。2026-10-07 09:29の「push済み」は09:30の訂正を維持する。

HTTPS Gitのgh credential helperが使うscopeはgist/read:org/repoでworkflowなし。NotesのOAuth App scope不足という原因と整合するが、当時のraw stderrを再読して拒否全文を独立確認したわけではない。前227 runはローカルcommit `16322263e3b09aa1c327f2823417b528802d9340` を検証・保存してBlocked終了した。

今回ownerが承認した既存SSH経路でGitHub boxp認証に成功した。SSHにはOAuth workflow scopeの属性はなく、登録先accountのrepository権限とpush policyが適用される。APIによるrepo権限はpush=trueだが、SSH read成功だけでwrite可能とは断定しない。再認証・新token・新鍵登録・scope追加は不要。通常pushとremote確認の実結果はObsidianの最終報告に記録する。

## 排他・PR状態・変更の存在場所

親223 run `20261007T092252Z-47acb8ba-1e71-418b-82ce-24cd80e2c1f0` は正規runner summaryで2026-10-07 09:31:53 UTC blocked終了。前227 run `20261013T040719Z-0af26e17-535f-4bc8-885f-f88914fa866f` も04:13:36 UTC blocked終了。223 lockはなく、locksには現在227のclaimのみ。親laneはBlocked。本227は正規runnerによりIn Progressになっている。

223 worktreeはclean/head `04a9b3954537c4e82e61a8a10f02b1f473b73bad`、branchの所有も旧223 worktreeのみ。前run worktreeも変更せず、現在run専用branchだけで作業した。runnerはticket単位原子lockで、global queue lockではない。同branch並行編集は観測されず、push直前にもremote head・223 tree・claimを確認する。force unlock・runner再起動・稼働作業中断・force-pushはしない。

| 項目 | 修復前の現在値 |
| --- | --- |
| state/merge/closed | OPEN / mergedAtなし / closedAtなし |
| Draft / autoMergeRequest | false / null |
| head | `04a9b3954537c4e82e61a8a10f02b1f473b73bad` |
| branch | `codex-task-board/BOXP-223-20261007T092252Z-47acb8ba-1e71-418b-82ce-24cd80e2c1f0` |
| PR API baseRefOid | `64534cec5e9c64d367346f009fcc96206df594f8` |
| 最新main ref | `72a16a7baae91f6b67824394c42481c46da816a4`（API baseと区別） |
| PR files | renovate.json、223 plan/validation/ci-followup.patch |
| CI | 旧head gitleaks SUCCESS、Chainsawなし（未実行はpassedではない） |

| 変更 | ローカル/remote履歴 | 修復前head本体/PR差分 |
| --- | --- | --- |
| Renovate検出と停止条件docs | ac355d8 / 89c2c52、remoteあり | あり |
| workflow実装 | 93ec140、remoteあり | revertでなし |
| revert | 1801ad3、remoteあり | 元workflowを復元 |
| 保存patch | b1d9fde / context調整04a9b395、remoteあり | docsのみ |
| 再適用 | 前run16322263、本runが再利用 | 修復対象の2 workflow本体と227 docs |

patch SHA-256は `f5a7a24cafb03421e2ed70a41abe49e752bf016b544862394a46e4226f345c6f`。修復前PR head04a9b395/当時の最新mainへの適用確認と適用後reverse check成功。確認時の最新mainでもApplication版は0.18.2、P2修正は未反映。mainの無関係commitを本PRへ混ぜない。

## 非秘密の認証診断と保存先

- OS user=boxp、uid=1000、HOME=/home/boxp、GH_CONFIG_DIR unset。gh既定領域は/home/boxp/.config/gh（中身を読まない）。GH_TOKEN/GITHUB_TOKEN/GIT_SSH_COMMAND/GIT_SSH/GIT_CONFIG_COUNTのoverrideなし。
- originは資格情報を含まないHTTPS URL。GitHub host helperはreset後gh auth git-credential。gh protocol preference sshと実origin HTTPSは別である。値全体・token行を表示しない。
- gh/APIは既存OAuthの読取経路を維持。git pushだけ明示宛先 `git@github.com:boxp/lolice.git` と本PR branch refspecを使用し、全repo insteadOf/helperやorigin/pushurlを書き換えない。
- SSH_AUTH_SOCK unset、ssh-add -lはagent接続なし。既存Host aliasなし、既定標準鍵/home/boxp/.ssh/id_ed25519が存在（600、boxp所有）、.sshは700、known_hostsは600。鍵内容・credential store・.env・Secret値・cookieは読まない。
- 公開fingerprint: `SHA256:Vxp20niPbgjMDpGFmE0HF06s+iJebtXw5NLTMOQoWhQ`（ED25519）。公開鍵fingerprintだけを取得した。
- `ssh -T -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10 git@github.com` はboxp認証成功の挨拶、exit1。既存known_hostsを変更せず厳密照合。未知hostの無検証承認なし。
- findmntで鍵の格納先は/home/boxpのLonghorn ext4 PVC。Deployment/READMEもworkspaceとrunnerが同じhome PVCをmountすることを確認。再作成を実施せず、同一PVCを維持する再作成では鍵/known_hosts/gh設定が残る設計と確認した。

OAuth/classic PATではworkflow scopeがworkflow更新に必要。GitHub App/fine-grained PATではrepository Contents(write)/Workflows(write)が該当する。今回は既存SSH account経路を使い、別資格情報の探索・注入や認証課金方式切替をしない。

公式根拠: [OAuth scopes](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps)、[SSH接続確認とexit1](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/testing-your-ssh-connection)、[Contents API権限](https://docs.github.com/en/rest/repos/contents#create-or-update-file-contents)、[gh環境](https://cli.github.com/manual/gh_help_environment)。

## 修正前後・push安全性

通常Chainsawは固定--version 0.18.2から、Applicationのspec.sources内chart=external-secretsのtargetRevision選択へ変更。固定semverだけ許可し、Git source mainや可変rangeを拒否する。

immutable preflightはdownload/install前にApplication版が既存pin 0.18.2と一致するか確認し、不一致なら停止する。既存digest `5d0645b815837af4ce1fd0436879e4adfe8230072103c65429c4a41d9e00f43d` とsha256sum拒否は保持。pin/digest同時更新の条件を緩和しない。

通常Chainsawはmain宛pull_request path trigger、workflow変更で3 components対象。preflightはworkflow_dispatchのみ。Applyは独立kind kubeconfig export/current-context guard後で実行され、本番接続経路なし。既存contents(read)/pull-requests(write)は結果comment用で、新規権限/secret参照/後段deployなし。

argocd-diffはargoproj path限定の読取diff、llm-sd-deployは別pathのmain push/manual、claudeは明示@claudeイベント、gitleaksはscan。今回workflow/docs pushで本番deployや権限拡張は発火しない。autoMergeなし、Renovate ESO automerge=falseも維持。merge/ready化・hold承認・gate緩和・無関係CI rerunはしない。

## 検証

| 検証 | 再開run結果 |
| --- | --- |
| python3 -B -m unittest discover -s tests/workflows -v | 15件成功 |
| actionlint 1.7.12 / 2 workflows | exit0 |
| 実shell+yq 4.54.1 | 9ケース成功 |
| 通常CI | 0.18.2/2.12.0選択、main/2.*拒否 |
| immutable | 0.18.2許可、2.12.0/main/2.*不一致拒否、改ざんarchive拒否 |
| patch / diff（修復前の検証） | 当時の最新main/修復前PR整合性、適用後reverse check、diff --check |
| 機密scan | 対象2 workflowと227 docsのみ、gitleaks redaction付きで実施 |

前run実shell scriptはworkspace validation/eso-version-selection.pyに保存、current repoを引数に再実行した。fixtureを使い本番clusterには接続しない。新digestのarchive検証ではない。remote CIの結果はObsidianに保存する。manual preflightは実施しない。

codex-reviewとcodex-review-fileは今回/home/boxp/.claude/skillsの既存skillを発見し使用する。terra unavailableのためworkerへ独立検証を委譲、P2以上なし。主担当が統合・最終判断を行う。Git管理下の実装はcodex-review、Git外の最終報告はcodex-review-fileの対象とする。Actions上のHelm/kind/yq可用性はremote結果で区別する。

## 親223の残ブロッカー

ESO High advisoryは未解消。承認済み管理経路とAWS SSM/STS限定egress方式・担当のowner判断は別途必要。CI修正/commit/push成功はmerge/本番適用やESO chart/RBAC/network/CRD/live Secret変更、storage移行、admission/egress/live試験の完了を意味しない。本runではそれらを再開しない。

## 反映済み状態とレビュー指摘対応

承認済みSSHで通常pushが成功し、PR headは04a9b395から
`104bedfee2200cbf674eb19bf4978627e65944d4` に更新された。
追加commitは実workflow修正16322263と認証診断文書104bedf。
remoteの2 workflow blobがローカルと一致し、PR差分に実workflow変更が存在する。
このheadのChainsaw run38346757016（detect/chainsaw）とgitleaks run38346757018はSUCCESS。
manual preflightは未実行。OPEN/未merge、autoMergeなし、MERGEABLEを確認した。

runnerレビューは適用済みpatchと再適用手順の陳腐化を指摘した。
今回の文書訂正で `docs/project_docs/BOXP-223/ci-followup.patch` を削除し、223のplan/validationを更新する。
上記のpatch hash/適用検証は修復前の歴史記録であり、現在のPR headへの再適用手順ではない。
履歴の04a9b395から当時のpatchを確認できる。後続headのCIとレビュー結果はObsidian最終報告へ追記する。


## 回帰テストのレビュー指摘（2026-10-13）

head3ce49a7のrunner gateは、既存15テストが新ESO分岐を実行しないP2を報告した。
前runの一時workspace上の実shell9ケースは手動検証の証拠であり、repositoryの継続的回帰テストではなかった。
複数sourceのApplication fixtureを使う実shellテストをtests/workflowsへ追加する。
通常Chainsawの既存unittest discoveryから実行し、chart source版選択、semver拒否、immutable不一致の取得前停止とdigest改ざん拒否を検証する。
workflow本体、既存版/digest、権限、認証設定は変更しない。新headとCI結果はObsidian最終報告へ記録する。

ローカル検証は実yq v4.44.3で全20件成功（既存15＋追加5）。テスト内のkubectl/helm/curl/kustomizeは記録用stubで、cluster/networkへ接続しない。
一致時の成功経路は期待digestを検査するsha256sum stub、改ざん拒否経路は実sha256sumを使用する。
workerはメモリ内でsource先頭誤選択/immutable guard削除へ退行させるとテストが失敗することも確認した。
対象3ファイルのgitleaks scanはfindings0、diff --check成功。yq v4はworkflow既存の依存であり、不足時はskipせずテストを失敗させる。
