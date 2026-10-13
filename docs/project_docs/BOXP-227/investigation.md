# BOXP-227: workflow push拒否と未反映CI修正の調査

確認日: 2026-10-13 UTC。対象: [boxp/lolice PR #818](https://github.com/boxp/lolice/pull/818)。

## 結論

ローカルcommitの失敗ではない。workflow変更のcommit `93ec140880d87e9f87d6ce9743272cff3412279e` は作成済みで、後にrevertとともにremote履歴へ到達している。PR headではworkflow本体が元へ戻り、保存patchだけが残る。2026-10-07 09:29の「push済み」は09:30の訂正を維持する。

現在のHTTPS Gitはgh credential helperを使用し、ghが報告するscopeに `workflow` がない。前回NotesのOAuth App scope不足によるpush拒否と整合する。前回のraw stderrや秘密を含み得るeventsは読まず、当時の拒否全文は独立再確認していない。今回も権限不足のpush再試行や新規認証は行っていない。

本run専用branchで保存patchを実workflowへ適用し検証した。remote反映は未実施で、P2はremote上では未解消。認証手続きが必要なためBlocked。

## 排他と現在のPR

親223 run `20261007T092252Z-47acb8ba-1e71-418b-82ce-24cd80e2c1f0` の通常runner `summary.edn` は `status=blocked/result=blocked/exit-code=0/finished-at=2026-10-07T09:31:53Z`。`locks/BOXP-223.edn` は存在せず、claimは解放済み。親laneはBlocked。既存223 worktreeはclean、headは下記PR headと一致する。既存worktree・共有checkoutを編集せず、unlock/restart/process中断を行っていない。

通常runner sourceはticket単位にlockを原子的取得し、確認時のlock一覧は本227 runのみ。global queue lockや別runnerを含む同branch排他を保証する仕組みではない。worktree一覧でPR branchの所有は旧223 worktreeだけ、本runは別branchを使う。同branchの並行編集は観測されていないが、他runnerでの不存在を保証しない。再確認時もPR head/旧treeは同じだった。

`gh pr view 818 --repo boxp/lolice --json ...` で次を確認した。

| 項目 | 現在値 |
| --- | --- |
| state / merge / closed | OPEN / mergedAtなし / closedAtなし |
| Draft / autoMergeRequest | false / null（過去のDraft記録より現在値を優先） |
| head | `04a9b3954537c4e82e61a8a10f02b1f473b73bad` |
| head branch | `codex-task-board/BOXP-223-20261007T092252Z-47acb8ba-1e71-418b-82ce-24cd80e2c1f0` |
| PR APIのbaseRefOid | `64534cec5e9c64d367346f009fcc96206df594f8` |
| main refの再確認値 | `72a16a7baae91f6b67824394c42481c46da816a4`（PR APIのbase値と区別） |
| changed files | `renovate.json` と `docs/project_docs/BOXP-223/{plan.md,validation.md,ci-followup.patch}` |
| CI | headのgitleaks SUCCESS。Chainsaw実行結果なし |

Chainsawは変更path filterに合致しないため、元PRで実行されていない。これはテストpassedではなく未実行。manual preflightも未実行。PR gate通過、merge、本番適用とは別に扱う。

## 変更の存在場所

| 変更 | ローカルcommit | remote履歴 | head本体 / PR最終差分 |
| --- | --- | --- | --- |
| Renovate検出と停止条件docs | `ac355d8` / `89c2c52` | あり | あり |
| workflow実装 | `93ec140` | あり（PR commits API確認） | `1801ad3`によるrevertでなし |
| revert | `1801ad36c01a09ea7a2dbd1b7a653f6b6cc4d66f` | あり | 元workflowを復元 |
| 未適用patch | `b1d9fde` / context調整`04a9b395` | あり | docs内にあり、workflow本体にはなし |
| 本run再適用 | 本run専用branchに保存 | まだなし | ローカル2 workflowと227 docsのみ |

`git show`、PR commits/files API、base/head間のworkflow diff（空）で確認した。patch SHA-256は `f5a7a24cafb03421e2ed70a41abe49e752bf016b544862394a46e4226f345c6f`。最新mainとPR headの両方へ `git apply --check` 成功。mainへ無関係なcommitを混ぜず、提供run branchのみをPR head起点にした。

main refをAPIで再取得したところPR APIのbaseRefOidより進んでいた。新しいmain objectをref更新なしで取得して確認し、既存kind CNI/RBAC等の追加と今回patchが競合しないことを検証した。最新mainでもESO Applicationは0.18.2、今回修正は未反映。最新mainをPRへmerge/rebaseしたわけではない。

## 非秘密の認証診断

| 項目 | 観測 / 意味 |
| --- | --- |
| origin | HTTPS / github.com / URL内資格情報なし |
| credential helper | GitHub host用helperはreset後 `gh auth git-credential`。値全体は表示しない |
| gh active account | boxp、認証status exit 0 |
| gh token scopes | gist / read:org / repo、workflowなし。token行は出力除外 |
| gh protocol preference | ssh。ただし実際のoriginはHTTPSなのでSSH利用の証拠にはならない |
| env override | GH_TOKEN / GITHUB_TOKEN / GH_HOST / GIT_ASKPASS / SSH_ASKPASS / GIT_SSH / GIT_SSH_COMMAND / GIT_CONFIG_COUNT / GIT_CONFIG_PARAMETERS / GH_CONFIG_DIRはunset |

gh/APIはCLIの認証選択、Git pushはremote transportとGit helper選択に従う。今回HTTPS helperがghへ委譲し、環境overrideもないため、scope不足が両者に関係する。API読取やdocs-only push成功はworkflow書込権限の証明にならない。

OAuth Appまたはclassic PATのworkflow変更には既存repo権限に加えて `workflow` scopeが必要。fine-grained PAT / GitHub Appでは対象repositoryのContents(write)とWorkflows(write)が該当する。SSHは登録済み鍵とrepository権限による別経路だが、使用可能な承認済み鍵は確認していない。今回のhistorical OAuth分類はNotesを根拠とし、token prefix/credential storeを読んで種別を再判定していない。

承認済み非秘密runner仕様/ソースにも、このrun専用の別workflow書込認証経路は確認できなかった。保存credentialの探索、GitHub App token発行、SSH切替、runnerや全repoのhelper設定変更は行わない。

根拠: [GitHub OAuth scopes](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps)、[GitHub Contents APIとworkflow権限](https://docs.github.com/en/rest/repos/contents#create-or-update-file-contents)、[gh環境選択](https://cli.github.com/manual/gh_help_environment)、[gh Git helper](https://cli.github.com/manual/gh_auth_setup-git)。

## 修正前後とpush安全性

通常Chainsawは以前 `--version 0.18.2` を固定していた。修正後はApplicationの `spec.sources` のうちchart=external-secretsのtargetRevisionを選び、固定semverのみ許可してHelmへ渡す。Git sourceのmainは選択しない。

immutable preflightはApplicationの版が既存pin `0.18.2` と一致するかをdownload/install前に確認する。既存digest `5d0645b815837af4ce1fd0436879e4adfe8230072103c65429c4a41d9e00f43d` と既存SHA-256拒否処理は保持する。版変更時にpinと検証済みdigestを同時更新する停止条件を維持する。

通常Chainsawはmain宛pull_requestのpath filter、preflightはworkflow_dispatchのみ。workflow変更は通常Chainsawの3 required componentsを対象にする。変更先のkubectl Applyは独立kind kubeconfigのguard後で、本番Applyではない。通常jobの既存contents(read)/pull-requests(write)は結果comment用、manual preflightはcontents(read)。新規権限・secret参照・依存後段を追加しない。

repoのargocd-diffはpull_requestのargoproj path対象の読取diff、llm-sd-deployは別path/main push、claudeは明示comment/review等、gitleaksはscan。workflow-only pushで本番deploy/Applyの経路は確認されない。PR autoMergeRequestはnull、ESO Renovate automerge=falseを保持する。scope追加はGitHub資格情報権限の人の判断であり、pushによる自動権限拡張ではない。PR merge/ready化・自動merge設定変更・hold承認・無関係CI rerunは行わない。

## 検証と制限

`python3 -B -m unittest discover -s tests/workflows -v`: 15件成功。report gateでskipped/errors/failuresをpassedへ繰り上げない既存test、immutable download拒否test、kind isolation/kubeconfig guard testを含む。

追加検証も成功した。

| 検証 | 結果 |
| --- | --- |
| actionlint 1.7.12 / 2 workflows | exit 0 |
| 実shell + yq 4.54.1 | 9ケース成功 |
| 通常CI | 0.18.2 / 2.12.0を選択、main / 2.*を拒否 |
| immutable preflight | 0.18.2のみ許可、2.12.0 / main / 2.*を拒否 |
| 既存sha256sum guard | 改ざんfixtureのarchiveを拒否 |
| gitleaks 8.30.1 / 今回4 files | findings 0、redact/fatalで秘密値非表示 |
| patch | PR head / 最新mainへの適用確認、適用後reverse check成功 |
| git diff --check | 成功 |

ツールは公式GitHub releaseからrepo外 `/tmp/boxp227-tools` へ取得し、公開checksum（yqはrelease asset API digest）を照合した。再現コマンドは `actionlint .github/workflows/chainsaw.yaml .github/workflows/chainsaw-preflight.yaml`、実shell検証は本run workspace直下の `validation/eso-version-selection.py`。shell検証はworkflowの実際の該当部分を抽出し、本番clusterに接続しないfixtureを使う。実archive取得による新digest検証ではない。

本run commit SHAはObsidian Projectsの同名報告に記録する。本書はcommit前に作成するため自身のcommit hashを含めない。`codex-review` / `codex-review-file` skillは提供catalogとskills/dotfiles検索で見つからず、低コストagentによる独立レビューを実施した。terra route unavailableのためlunaへ調査・レビュー・検証を委譲し、主担当がscopeと最終差分を判断した。

独立レビュー: P2以上なし。workflowの既存yq利用を前提とするため、実Actions環境でのyq可用性やHelm/kindの実行成功はローカルsource reviewだけでは確認できない。

remote workflow反映、新headのGitHub Chainsaw結果、manual kind preflightは認証待ちで未実施。local test成功はremote CIの代替ではない。

## 人による再開手順

既存OAuth経路を継続する場合、ownerが本workspaceの公式GitHub CLIで `gh auth refresh --hostname github.com --scopes workflow` を実行し、表示される公式GitHub device/login画面でboxpアカウントと追加workflow権限を確認して承認する。agentはこのコマンドを実行しない。追加scopeの最小値はworkflowで、admin系scopeや新しい広いtokenは不要。OAuth scope自体はrepo限定にできないため、操作対象はloliceの本PRだけに限定する。repo限定資格情報が必要ならowner側で方式を判断する。

完了後はtoken/コードをチャットに貼らず、非秘密のscope確認だけを行う。通常Task Board helperで227の再開を依頼する。再開runは排他/現PR状態/headを再確認し、現headが同じなら本runのCI/docs限定commitを既存PR branchへ通常fast-forward pushする。headが進んでいれば差分を最新headへ載せ直し、再検証する。force-pushや旧worktree編集はしない。成功後remote workflow blobとPR差分、対応Chainsawの実際のpassed/skipped/failedを確認する。

公式手順: [gh auth refresh](https://cli.github.com/manual/gh_auth_refresh)。既存Codex subscription認証は変更しない。

## 親223の残ブロッカー

ESO High advisoryは未解消、承認済み管理経路とAWS SSM/STS限定egress方式・担当のowner判断が別途必要。CI修正はESO chart/RBAC/network/CRD/live Secret変更、storage移行、admission/egress/live試験の実施を意味しない。
