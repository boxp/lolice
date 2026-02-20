# T-20260220-006: OpenClaw Docker image 更新責務分離

## 目的

OpenClaw Docker image更新の責務を整理し、boxp/lolice は ArgoCD Image Updater に一本化、boxp/arch は Renovate で自動更新できる構成にする。

## 変更内容

### boxp/lolice

1. **renovate.json**: OpenClaw向けcustomManager（regex）を削除。packageRulesでOpenClawイメージを`enabled: false`に設定（ArgoCD Image Updater一本化）
2. **docs/renovate-openclaw-ops.md**: 責務分離ルールを明記したドキュメントに更新
3. **docs/openclaw-image-update-ops.md**: ArgoCD Image Updater一本化を反映した運用手順に更新

### boxp/arch

1. **renovate.json5**: OpenClawベースイメージ(`ghcr.io/openclaw/openclaw`)向けpackageRuleを追加（automerge無効、ラベル付与）
2. **docs/openclaw-image-update-responsibility.md**: 責務分離ルールドキュメントを新規追加

## リスク

- lolice側でRenovateが既にOpenClawの更新PRを作成していた場合、そのPRはクローズが必要
- arch側のDockerfile `FROM`行はRenovateの組み込みdockerfileマネージャーで自動検出されるため、customManagerは不要
