# BOXP-263: lolice main 保護（ruleset「block main」）有効化 計画と記録

## 目的

boxp/lolice の default branch `main` に対する未許可の直 push・force push・削除を GitHub ruleset で防ぎ、GitOps の変更経路を PR 経由に限定する。設定変更は GitHub 側（リポジトリ内のコードではない）で行い、本ディレクトリにはスナップショットと記録を置く。

## 調査結果（2026-10-22）

- ruleset 509073「block main」は 2024-03-10 作成のまま `enforcement: disabled`。branch protection（旧方式）なし。effective rules は空。
- main へ直 push する automation は argocd-image-updater のみ（deploy key `argocd-image-updater@lolice`、write、fast-forward push）。
- renovate / codex-task-board / Claude Code Action / llm-sd-deploy は PR 経由。
- CI: argocd-diff（pull_request、`argoproj/**` のみ）、gitleaks（pull_request と main への push）。

## 変更内容（最小）

ruleset 509073 を PUT で更新:

- `enforcement`: disabled → active
- rules: deletion, non_fast_forward（既存）+ `pull_request`（required_approving_review_count 0、merge/squash/rebase 許可）
- bypass_actors: `DeployKey` / always（image-updater 用。ただし GitHub 仕様で全 deploy key に適用される。「受容した例外」節参照）。admin bypass は付けない。
- 対象 refs: `~DEFAULT_BRANCH` のまま。
- required status checks は追加しない（argocd-diff が path 限定のため docs-only PR がマージ不能になる）。

スナップショット: `ruleset-509073-before.json` / `ruleset-509073-after.json`。

## 実効確認

1. `GET /repos/boxp/lolice/rules/branches/main` に 3 ルールが載ることを確認。
2. fixture: ruleset の対象に `refs/heads/ruleset-fixture/**` を一時追加し、branch `ruleset-fixture/boxp-263` で以下を試験。
   - boxp token の fast-forward 直 push → 拒否（Changes must be made through a pull request）
   - force push → 拒否
   - branch 削除 → 拒否（Cannot delete this branch）
   - 一時 deploy key からの push → Bypassed で成功
3. 後片付け: 対象を main のみに戻す、一時 deploy key 削除、fixture branch 削除。
4. 本 PR 自体で「通常 PR + CI（gitleaks）+ merge 可能」を確認する。

main には push・force push・削除のいずれも行っていない。

## 受容した例外: DeployKey bypass の範囲（2026-10-22 追記、codex-review 指摘対応）

GitHub ruleset の bypass actor `DeployKey` は `actor_id: null` 固定で、**特定の deploy key に限定できない**（[Repository rules API](https://docs.github.com/en/rest/repos/rules)）。したがって現在の bypass は「リポジトリの全 deploy key（将来追加分を含む）」に対して main の直 push / force push / 削除を許す。これは image-updater 専用の例外にはなっておらず、以下の理由と統制のもとで **明示的に受容** する。

受容理由:

- deploy key の追加は repo admin 権限が必要で、admin は ruleset 自体を無効化・編集できる。したがって「新しい deploy key を足して迂回する」のは、今回の保護が防ぐ対象（write 権限 token の漏洩、誤操作、PR 経路を通らない変更）より強い権限を前提とし、新しい権限昇格経路にはならない。
- 現実的なリスクは「既存 deploy key の秘密鍵の漏洩」。鍵は AWS Parameter Store（`argocd-image-updater-lolice-github-secret-key`）→ ESO → argocd namespace の `repo-lolice` Secret にのみ存在し、GitHub Actions secrets には含めていない。
- 今回の変更前は main に保護が一切なく、deploy key に限らず全 write 権限者が直 push 可能だった。本例外を残しても保護範囲は純増。
- 代替案（GitHub App 化）は App 作成が GitHub UI 操作を要し、owner 作業と image-updater の認証方式変更（ESO/ArgoCD repo credential を App ID / Installation ID / private key に差し替え）を伴うため、本票の「最小設定変更で即保護」には含めない。

管理（統制）:

| 統制 | 内容 |
| --- | --- |
| 台帳 | 許可 deploy key は **1 本のみ**: id 141307619 `argocd-image-updater@lolice`（write、2026-01-26 作成）。これ以外は存在してはならない。 |
| 検知 | 日次セキュリティ診断（Obsidian `Projects/monthly-security-audit/runbook`）で `gh api repos/boxp/lolice/keys` を id/title/read_only に絞って台帳と照合し、未登録 key が存在すれば High として起票。ruleset 509073 の `bypass_actors` が `DeployKey/always` 1 件のままであることも同時に確認する。 |
| 縮小方針 | 新しい自動化で main へ直 push する用途に deploy key を追加しない。必要なら PR 経由（pull_request ルール、approval 0）を使う。 |
| 恒久対応 | image-updater の write-back を GitHub App 認証へ移行し、bypass actor を `Integration`（App 単位で識別可能）に置き換える。後続チケット BOXP-265 で owner 判断。 |

## rollback

- GitHub UI https://github.com/boxp/lolice/rules/509073 で Enforcement を Disabled にする。
- または `before.json` の内容（name/target/enforcement/conditions/rules/bypass_actors）で PUT し直す。

## 残件

- required status checks（gitleaks）の追加要否。
- `require_extra_approval_for_unattributed_changes: true`（GitHub 既定で付与）が lolice の PR マージを阻害しないか監視。
- image-updater 本物の deploy key での push は次回イメージ更新時に観測する。
- DeployKey bypass が全 deploy key に及ぶ点は上記のとおり受容。恒久対応（GitHub App 化）は BOXP-265 で扱う。

詳細な記録は Obsidian `Projects/BOXP-263/README.md`。
