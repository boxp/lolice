# BOXP-265: argocd-image-updater の write-back を GitHub App 認証へ移行し、main ruleset の DeployKey bypass を Integration bypass に置き換える

チケット: Obsidian `Tickets/BOXP-265`。背景は `docs/project_docs/BOXP-263/plan.md`「受容した例外」節。

## 目的

- `boxp/lolice` main への image-updater の直 push を、全 deploy key に効く `DeployKey` bypass ではなく、専用 GitHub App（`Integration` actor）単位の bypass に限定する。
- 旧認証（deploy key `argocd-image-updater@lolice`、SSM `argocd-image-updater-lolice-github-secret-key`、Secret `repo-lolice`）は App 認証で実 write-back が成功するまで残し、成功後に撤去する。

## 調査結果（2026-10-22）

- live: image-updater `v1.4.0`、repo-server `v3.5.6`、`argocd` / `argocd-image-updater` は Synced / Healthy。
- image-updater v1.4.0 `pkg/argocd/gitcreds.go` の `getCredsFromSecret`: `writeBackConfig.gitConfig.repository`（未指定なら Application の repoURL）が SSH URL なら Secret の `sshPrivateKey`、HTTPS URL なら `githubAppID` / `githubAppInstallationID` / `githubAppPrivateKey`（または `username` / `password`）を読む。Secret に Argo CD の repository ラベルや `url` は不要。
- 旧認証の利用先:
  - ImageUpdater 11 件すべてが `git:secret:argocd/repo-lolice`。7 件は `gitConfig.repository: git@github.com:boxp/lolice.git` を明示、4 件（ark-discord-bot / ark-survival-ascended / palserver / palserver-2）は未指定で Application の repoURL（SSH）を使う。
  - Argo CD 側: 上記 4 Application の `repoURL` が `git@github.com:boxp/lolice.git` のため、`repo-lolice`（repository ラベル付き・SSH）を fetch にも使っている。他の Application は HTTPS で匿名 fetch。
- ESO: ClusterSecretStore `parameterstore`（ap-northeast-1、`external_secrets_role`）は Ready。owner が作成済みの SSM `/lolice/argocd-image-updater/{APP_ID,INSTALLATION_ID,PRIVATE_KEY}`（SecureString）は、ARC の `/lolice/arc/github-app-*` と同じパス形式で参照する。
- ruleset 509073: `deletion` / `non_fast_forward` / `pull_request` / `required_status_checks(gitleaks, status-check)`（BOXP-266 が 2026-10-22 06:05 UTC に追加）、bypass は `DeployKey / always` のみ。
- 実行環境（codex-workspace ServiceAccount）では Secret の読取・controller ログ取得・AWS CLI が無い。秘密値を読まない検証経路を使う: ESO は ExternalSecret の `Ready` / `SecretSynced` 条件、App による push は GitHub の PushEvent actor（`<app-slug>[bot]`）と deploy key `last_used` の不変、App ID は公開 API `GET /apps/<slug>`。

## 変更（段階）

### Phase 1（本 PR）: App 認証 Secret の追加のみ。挙動変更なし

- `argoproj/argocd/base/external-secret-repo-github-app.yaml`: ExternalSecret `external-secret-repo-lolice-github-app` → Secret `argocd/repo-lolice-github-app`（`githubAppID` / `githubAppInstallationID` / `githubAppPrivateKey`）。Argo CD の repository ラベルは付けない（Application の fetch 経路を変えず、App 認証の影響範囲を write-back に限定）。
- `argoproj/argocd/kustomization.yaml` に追加。
- 確認: `kubectl kustomize argoproj/argocd` の差分が当該 ExternalSecret のみ。merge 後に ExternalSecret が `Ready=True / SecretSynced` になることで、ESO ロールの SSM/KMS 読取と 3 パラメータの存在を秘密値を見ずに検証する。

### Phase 2: write-back を App 認証へ切替

- ImageUpdater 11 件: `method: git:secret:argocd/repo-lolice-github-app`、`gitConfig.repository: https://github.com/boxp/lolice.git` を明示。
- Application 4 件（ark-discord-bot / ark-survival-ascended / palserver / palserver-2）: `repoURL` を `https://github.com/boxp/lolice` に変更し、Argo CD fetch の deploy key 依存をなくす。
- `.claude/skills/lolice-image-updater/SKILL.md` の手順を更新。
- 確認: 正当な image 更新による main への push が PushEvent actor `<app-slug>[bot]` で記録され、deploy key `last_used` が進まないこと。無関係な image 更新は作らない。**この確認は Phase 3a（Integration bypass の先行追加）の後でないと成立しない**（下記訂正）。

### Phase 3（順序訂正 2026-10-22 09:30 UTC）: ruleset の bypass を App の Integration へ移行（GitHub 設定、コード外）

**訂正理由**: main の effective rules は `pull_request` + `required_status_checks(gitleaks, status-check)` で、bypass は `DeployKey / always` のみ（2026-10-22 09:25 UTC に `GET /repos/boxp/lolice/rules/branches/main` と ruleset 509073 で再確認、classic branch protection は無し）。Integration は DeployKey bypass を継承しないため、Phase 2 以降の App 認証 push は bypass 無しで `pull_request` ルールに掛かり拒否される。「App push を待ってから Integration bypass を設定する」は循環するので、**旧 DeployKey bypass を保持したまま App の Integration bypass を先に追加する**（可逆な中間段階）。拒否を実測したわけではない（Phase 2 反映後に正当な image 更新がまだ発生していない）。

- 3a. `scripts/ruleset-integration-bypass.sh add <app-slug>`（dry-run）→ `--apply`: `GET /apps/<slug>` の `id`（Integration actor_id、Installation ID ではない）を `bypass_actors` に **追加**。DeployKey は残す。rules / conditions / enforcement は直前 GET をそのまま使う。
- 3b. 正当な image 更新で main への push が PushEvent actor `<app-slug>[bot]` で記録され、deploy key `last_used` が進まないことを確認（Phase 2 の確認項目）。
- 3c. `scripts/ruleset-integration-bypass.sh app-only <app-slug> --apply`: DeployKey bypass を除去し Integration のみにする。再度 App 主体の push 成功を確認してから Phase 4 へ。
- 前提: 対象 App の slug（または App ID）。実行環境は Secret `argocd/repo-lolice-github-app` を読めず、`GET /user/installations` も gh の OAuth token では 403 のため、**owner から App の slug（`https://github.com/settings/apps/<slug>` の末尾、非秘密）の提示が必要**。スクリプトは `GET /apps/<slug>` で owner が `boxp` であることを検証してから使う。
- スクリプトは毎回 `/tmp/ruleset-509073-<timestamp>.json` に直前 GET を保存し、`restore <file> --apply` で戻せる。

### Phase 4: 旧認証の撤去

- `external-secret-repo-lolice`（Secret `repo-lolice`）を削除する PR。
- deploy key 141307619 `argocd-image-updater@lolice` を削除（用途が上記のみであることを確認済みの上で）。
- SSM `argocd-image-updater-lolice-github-secret-key` の削除は AWS 操作のため owner に依頼。

## rollback

- Phase 1: PR を revert（旧認証は無変更）。
- Phase 2: PR を revert すると `repo-lolice`（SSH）に戻る。Phase 4 前なら旧 deploy key は生きている。
- Phase 3: `scripts/ruleset-integration-bypass.sh restore <直前GETのjson> --apply`（`bypass_actors` に DeployKey を戻す）。3a の段階では DeployKey bypass が残っているため、App 側で問題があっても PR #872 の revert で SSH 経路に戻れる。
- Phase 4 以降は旧鍵を復活できないため、Phase 2〜3 の確認を終えてから実施する。

## 安全境界

- App 認証での push 成功前に旧 deploy key / DeployKey bypass を削除しない。
- 共有 credential（`regcred`、ARC の App 等）は触らない。main 保護・required checks を弱めない。
