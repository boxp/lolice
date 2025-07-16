# ArgoCDのApp of Appsパターンへの移行設計書

## 概要

loliceプロジェクトにおいて、ArgoCDアプリケーションを個別管理から「App of Apps」パターンによる宣言的管理に移行する。これにより、新規アプリケーションの自動追加機能を実現し、PR時の差分表示を改善する。

## 背景と課題

### 現状の問題点

1. **新規アプリケーション追加時の課題**
   - ArgoCDに未登録のアプリケーションはdiffが表示されない
   - マージ後に手動で`argocd app create`を実行する必要がある

2. **管理の複雑性**
   - 各アプリケーションが個別にArgoCDに登録されている
   - 一元的な管理が困難

3. **GitOpsの原則との乖離**
   - ArgoCDアプリケーション自体がGitで管理されていない
   - 宣言的でない管理方法

## 解決策: App of Appsパターン

### アーキテクチャ

```
argoproj/
├── argocd-apps/                    # 親アプリケーション
│   └── application.yaml            # App of Apps定義
├── kustomization.yaml              # 全application.yamlの集約
├── ark-discord-bot/
│   ├── application.yaml            # 個別アプリケーション
│   └── ...
├── hitohub/
│   └── overlays/
│       ├── prod/
│       │   └── application.yaml
│       └── stage/
│           └── application.yaml
└── ...
```

### 動作原理

1. **App of Apps (argocd-apps)**がすべてのapplication.yamlを監視
2. 新規application.yamlが追加されると自動的にArgoCDに反映
3. GitOpsの宣言的管理を実現

## 実装詳細

### 1. App of Appsアプリケーション

**argoproj/argocd-apps/application.yaml:**
```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: argocd-apps
  namespace: argocd
  finalizers:
    - resources-finalizer.argocd.argoproj.io
spec:
  project: default
  source:
    repoURL: https://github.com/boxp/lolice.git
    path: argoproj
    targetRevision: main
  destination:
    server: https://kubernetes.default.svc
    namespace: argocd
  syncPolicy:
    automated:
      prune: false  # 重要: 既存リソースを削除しない
      selfHeal: true
    syncOptions:
    - CreateNamespace=false
```

**重要なポイント:**
- `prune: false`: 既存の個別管理されているアプリケーションを削除しない
- `selfHeal: true`: Gitの変更を自動的に反映

### 2. Kustomize設定

**argoproj/kustomization.yaml:**
```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

resources:
# 全てのapplication.yamlを収集
- argocd-image-updater/application.yaml
- argocd/application.yaml
- ark-discord-bot/application.yaml
- ark-survival-ascended/application.yaml
- boxp-home/application.yaml
- calico/application.yaml
- descheduler/application.yaml
- external-secrets-operator/application.yaml
- hitohub/overlays/prod/application.yaml
- hitohub/overlays/stage/application.yaml
- k8s-ecr-token-updater/application.yaml
- k8s/application.yaml
- kubernetes-dashboard/application.yaml
- local-volume-provisioner/application.yaml
- longhorn/application.yaml
- openhands/application.yaml
- palserver/application.yaml
- prometheus-operator-crd/application.yaml
- prometheus-operator/application.yaml
- reloader/application.yaml
- tidb-operator/application.yaml
```

**重要な変更点:**
- kustomization.yamlの場所を`argoproj/argocd-apps/`から`argoproj/`に変更
- kustomizeのセキュリティポリシーに対応するため、相対パスを使用

### 3. 自動更新スクリプト

**scripts/update-app-of-apps.sh:**
```bash
#!/bin/bash
set -e

KUSTOMIZATION_FILE="argoproj/kustomization.yaml"

echo "Updating App of Apps kustomization.yaml..."

# kustomization.yamlを生成
cat > $KUSTOMIZATION_FILE << 'EOF'
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

resources:
EOF

# application.yamlを検索して追加（argocd-apps自身は除外）
find argoproj -name "application.yaml" -not -path "argoproj/argocd-apps/*" | \
  sort | \
  sed 's|^argoproj/||' | \
  sed 's/^/- /' >> $KUSTOMIZATION_FILE

echo "Updated $KUSTOMIZATION_FILE with $(grep -c '^-' $KUSTOMIZATION_FILE) applications"
```

### 4. GitHub Actions統合

#### 修正版 argocd-diff.yaml

既存のargocd-diff.yamlファイルの以下の部分を修正します：

**修正前（現在の実装）:**
```yaml
# 変更があったアプリケーションのdiffを取得
FOUND_CHANGES=false
for dir in $CHANGED_DIRS; do
  # ディレクトリツリーを上に遡って最も近いapplication.yamlを持つディレクトリを探す
  CURRENT_DIR=$dir
  while [[ "$CURRENT_DIR" == argoproj* ]]; do
    if [ -n "${APP_INFO[$CURRENT_DIR]}" ]; then
      APP_NAME="${APP_INFO[$CURRENT_DIR]}"
      echo "Changes detected for application: $APP_NAME (in $CURRENT_DIR)"
      FOUND_CHANGES=true
      
      # Diffの結果を追記
      echo "### アプリケーション: $APP_NAME の差分" >> app_diff_results.md
      echo "パス: $CURRENT_DIR" >> app_diff_results.md
      echo '```diff' >> app_diff_results.md
      
      # Diffを取得
      argocd app diff "argocd/$APP_NAME" \
        --header "CF-Access-Client-Id: $CF_ACCESS_CLIENT_ID,CF-Access-Client-Secret: $CF_ACCESS_CLIENT_SECRET" \
        --grpc-web \
        --insecure \
        --local-repo-root "$REPO_ROOT" \
        --local "$REPO_ROOT/$CURRENT_DIR" 2> >(grep -v "local diff without --server-side-generate is deprecated" >&2) >> app_diff_results.md
      
      # ... 以下既存の処理
    fi
  done
done
```

**修正後（App of Apps対応）:**
```yaml
# 変更があったアプリケーションのdiffを取得
FOUND_CHANGES=false
for dir in $CHANGED_DIRS; do
  # ディレクトリツリーを上に遡って最も近いapplication.yamlを持つディレクトリを探す
  CURRENT_DIR=$dir
  while [[ "$CURRENT_DIR" == argoproj* ]]; do
    if [ -n "${APP_INFO[$CURRENT_DIR]}" ]; then
      APP_NAME="${APP_INFO[$CURRENT_DIR]}"
      echo "Changes detected for application: $APP_NAME (in $CURRENT_DIR)"
      FOUND_CHANGES=true
      
      # App of Appsで管理されているか確認
      RELATIVE_APP_PATH="$CURRENT_DIR/application.yaml"
      APP_IN_KUSTOMIZE=$(grep -c "$RELATIVE_APP_PATH" argoproj/argocd-apps/kustomization.yaml 2>/dev/null || echo "0")
      
      if [ "$APP_IN_KUSTOMIZE" -gt 0 ]; then
        MANAGEMENT_TYPE="App of Apps管理"
      elif argocd app get "argocd/$APP_NAME" > /dev/null 2>&1; then
        MANAGEMENT_TYPE="直接管理"
      else
        MANAGEMENT_TYPE="新規アプリケーション"
      fi
      
      # Diffの結果を追記
      echo "### アプリケーション: $APP_NAME の差分 ($MANAGEMENT_TYPE)" >> app_diff_results.md
      echo "パス: $CURRENT_DIR" >> app_diff_results.md
      echo '```diff' >> app_diff_results.md
      
      # Diffを取得（既存の処理と同じ）
      REPO_ROOT=$(pwd)
      set +e
      argocd app diff "argocd/$APP_NAME" \
        --header "CF-Access-Client-Id: $CF_ACCESS_CLIENT_ID,CF-Access-Client-Secret: $CF_ACCESS_CLIENT_SECRET" \
        --grpc-web \
        --insecure \
        --local-repo-root "$REPO_ROOT" \
        --local "$REPO_ROOT/$CURRENT_DIR" 2> >(grep -v "local diff without --server-side-generate is deprecated" >&2) >> app_diff_results.md
      DIFF_EXIT_CODE=$?
      set -e
      
      # exit codeに基づいて適切なメッセージを追加（既存の処理と同じ）
      if [ $DIFF_EXIT_CODE -eq 0 ]; then
        echo "✅ 差分なし" >> app_diff_results.md
      elif [ $DIFF_EXIT_CODE -eq 1 ]; then
        echo "ℹ️ 上記の差分が見つかりました" >> app_diff_results.md
      elif [ $DIFF_EXIT_CODE -eq 2 ]; then
        echo "❌ エラーが発生しました" >> app_diff_results.md
        exit 1
      fi
      
      echo '```' >> app_diff_results.md
      echo "" >> app_diff_results.md
      break
    fi
    
    # 親ディレクトリに移動
    CURRENT_DIR=$(dirname "$CURRENT_DIR")
  done
done
```

#### 主な変更点

1. **管理タイプの判定ロジック追加**
   - `argoproj/argocd-apps/kustomization.yaml`をチェック
   - App of Apps管理、直接管理、新規アプリケーションを判定

2. **差分表示の改善**
   - 管理タイプを表示に含める
   - 新規アプリケーションでもdiffが正常に表示される

3. **既存の処理を維持**
   - 既存のエラーハンドリングやメッセージ表示はそのまま
   - Cloudflare Accessの認証処理も維持

**注意点:**
- 新規アプリケーションの場合、`argocd app diff`はエラーになる可能性がある
- その場合は`kustomize build`の結果を表示するなどの追加処理が必要になる場合がある

#### 実装上の変更点

実際の実装では、以下の変更が必要でした：

1. **kustomization.yamlの配置場所**
   - 当初予定: `argoproj/argocd-apps/kustomization.yaml`
   - 実際の実装: `argoproj/kustomization.yaml`
   - 理由: kustomizeのセキュリティポリシーが親ディレクトリのファイル参照を制限するため

2. **App of Appsのソースパス**
   - 当初予定: `path: argoproj/argocd-apps`
   - 実際の実装: `path: argoproj`
   - 理由: kustomization.yamlの配置場所変更に伴う調整

3. **パスの生成方法**
   - 相対パス（`../`）から絶対パス（`argoproj/`から見た相対パス）に変更
   - kustomizeのセキュリティ制約に対応

4. **検出されたアプリケーション数**
   - 実際に21個のアプリケーションを検出・管理対象に追加

## 移行手順

### Step 1: App of Appsの準備

```bash
# 1. ディレクトリ作成
mkdir -p argoproj/argocd-apps

# 2. スクリプトの実行権限付与
chmod +x scripts/update-app-of-apps.sh

# 3. kustomization.yamlの生成
./scripts/update-app-of-apps.sh
```

### Step 2: App of Appsのデプロイ

```bash
# 1. 変更をコミット
git add argoproj/argocd-apps/ argoproj/kustomization.yaml scripts/
git commit -m "Add App of Apps configuration"
git push

# 2. App of Appsを手動で作成（初回のみ）
argocd app create argocd-apps \
  --repo https://github.com/boxp/lolice.git \
  --path argoproj \
  --dest-server https://kubernetes.default.svc \
  --dest-namespace argocd \
  --sync-policy automated \
  --auto-prune=false

# 3. 同期を実行
argocd app sync argocd-apps
```

### Step 3: GitHub Actionsの更新

```bash
# 1. argocd-diff.yamlの修正
# 2. 変更をコミット・プッシュ
```

## 運用方法

### 新規アプリケーションの追加

1. **通常の手順でapplication.yamlを作成**
2. **kustomization.yamlに新規アプリケーションを追加**（`./scripts/update-app-of-apps.sh`を実行）
3. **PRを作成** → 通常のdiffが表示される
4. **マージ** → ArgoCDが自動同期して新規アプリケーションが自動追加

### 既存アプリケーションの管理

- 既存の個別管理アプリケーションはそのまま動作
- 段階的にApp of Apps管理に移行可能
- `prune: false`により既存リソースは影響を受けない

## リスクと対策

### リスク

1. **App of Appsの障害**
   - App of Appsが動作しなくなると新規アプリケーションが追加されない
   - 既存アプリケーションは影響を受けない

2. **kustomization.yamlの同期ミス**
   - 手動でapplication.yamlを追加してもkustomization.yamlが更新されない場合

### 対策

1. **監視とアラート**
   - App of Appsの同期状態を監視
   - GitHub Actionsの失敗を通知

2. **フォールバック**
   - 手動でkustomization.yamlを更新可能
   - 従来の個別管理に戻すことも可能

3. **テスト環境での検証**
   - 移行前にテスト環境で動作確認

## メリット

1. **自動化**
   - 新規アプリケーションの自動追加
   - 手動操作の削減

2. **GitOpsの実現**
   - 宣言的な管理
   - Git経由での一元管理

3. **ダウンタイムゼロ**
   - 既存アプリケーションへの影響なし
   - 段階的な移行が可能

4. **スケーラビリティ**
   - アプリケーション数の増加に対応
   - 統一的な管理方法

## 今後の展開

1. **ApplicationSetの検討**
   - より動的なアプリケーション管理
   - テンプレート化の推進

2. **環境別管理**
   - dev/staging/prod環境の分離
   - 環境固有の設定管理

3. **モニタリング強化**
   - アプリケーション状態の可視化
   - 自動復旧機能の実装

## 結論

App of Appsパターンの導入により、ArgoCDアプリケーションの管理が大幅に改善される。既存環境への影響を最小限に抑えながら、GitOpsの原則に基づいた宣言的管理を実現できる。