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
- bypass_actors: `DeployKey` / always（image-updater 用）。admin bypass は付けない。
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

## rollback

- GitHub UI https://github.com/boxp/lolice/rules/509073 で Enforcement を Disabled にする。
- または `before.json` の内容（name/target/enforcement/conditions/rules/bypass_actors）で PUT し直す。

## 残件

- required status checks（gitleaks）の追加要否。
- `require_extra_approval_for_unattributed_changes: true`（GitHub 既定で付与）が lolice の PR マージを阻害しないか監視。
- image-updater 本物の deploy key での push は次回イメージ更新時に観測する。

詳細な記録は Obsidian `Projects/BOXP-263/README.md`。
