# BOXP-264: インフラ安全性トリアージと低リスク対策の反映

## 目的

公開 GitOps repo (boxp/lolice, boxp/arch) の変更権限・CI・適用経路・秘密管理、Cloudflare Access/Tunnel と LAN/Tailscale の入口、workload 境界、更新・復旧能力を read-only でトリアージし、即時かつ低リスクに反映できる対策だけをこの票で反映する。包括的な安全宣言はしない。

## 調査範囲と結論の置き場所

- 入口ノート: Obsidian `Projects/BOXP-264/README.md` (目的 / 現状 / 安全境界 / 優先順位 / 即実施した変更と証拠 / 残件 / owner の次の一判断)
- 既存の日次診断 (`Projects/monthly-security-audit`) と是正票 (BOXP-263 main 保護、BOXP-259 Tailscale、BOXP-233 診断設計) を前提にし、重複起票・新規 cron・新規 scanner は作らない。

## この PR で反映する変更 (lolice)

1. `.github/workflows/argocd-diff.yaml` の job permissions を最小化する。
   - 変更前: `contents: write`, `pull-requests: write`, `repository-projects: write`, `id-token: write`
   - 変更後: `contents: read`, `pull-requests: write`, `id-token: write`
   - 根拠: この job は checkout と `argocd app diff` と PR コメント投稿のみで commit/push をしない。`pull_request` トリガーのため fork からは secrets が渡らないが、PR の内容次第で repo 内 token の権限が広いほど影響が大きいので縮小する。
   - 影響: PR コメントは `pull-requests: write` で継続。Tailscale WIF は `id-token: write` で継続。

## PR 外で即時反映した設定変更 (GitHub repo settings、両 repo)

- Actions の fork PR 承認ポリシーを `first_time_contributors` → `all_external_contributors` に変更 (boxp/lolice, boxp/arch)。
  - 根拠: 直近 300 PR に fork 由来 (cross repository) は 0 件。Renovate / tfaction は in-repo branch で動く GitHub App のため影響なし。arch は `pull_request_target` で plan を回しているため fork 由来の自動実行を人の承認待ちにする価値が高い。
  - 戻し方: 同 API に `first_time_contributors` を PUT。

## この票で変更しない (owner 判断 / 別票)

- Cloudflare Access の人向け policy が IaC 上 `include = login_method(GitHub IdP)` のみで email/require 制限がない件 (arch terraform)。identity rule の値 (email / group) の決定が必要なため owner 判断。
- `sha_pinning_required` 有効化 (lolice の tag 参照 17 件を先に SHA 化する必要がある)。
- `allowed_actions: all` の絞り込み、external-secrets namespace の NetworkPolicy、apiserver audit/encryption。

## 確認

- lolice: PR 上で gitleaks / chainsaw (該当 path のみ) が通ること。argocd-diff は argoproj/** 変更時のみ走るため、次に argoproj を触る PR で PR コメントが付くことを確認する (本 PR では未検証)。
- GitHub 設定: `GET /repos/{repo}/actions/permissions/fork-pr-contributor-approval` が `all_external_contributors` を返すこと (変更直後に確認済み)。
