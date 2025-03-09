# argoproj配下のK8sマニフェスト検証GitHub Action 設計書

## 1. 目的

loliceリポジトリの`argoproj`ディレクトリ配下にあるKubernetesマニフェストファイルの構文的・意味的な正当性を検証するGitHub Actionを追加し、プルリクエスト時およびメインブランチへのプッシュ時に自動検証を行う仕組みを構築する。これにより、マニフェストの品質を向上させ、本番環境へのデプロイ前に問題を早期発見できるようにする。

## 2. 背景

現在のloliceリポジトリには、`argocd-diff.yaml`というワークフローが存在し、`argoproj`ディレクトリに変更があった場合に、ArgoCDを使用して変更差分を確認しPRにコメントする機能を提供している。しかし、このワークフローはdiffの表示に特化しており、マニフェストの検証機能は含まれていない。

マニフェストのエラーはクラスタにデプロイされた時点で初めて明らかになるケースが多く、この検証ステップをCIプロセスに組み込むことで、問題を早期に発見し開発サイクルを短縮できる。

## 3. 実装方法

### 3.1 追加するファイル

新しいGitHub Actionsワークフローファイルを作成する：

**ファイルパス**: `lolice/.github/workflows/validate-k8s-manifests.yaml`

```yaml
name: Validate Kubernetes Manifests

on:
  pull_request:
    paths:
      - 'argoproj/**'
  push:
    branches: [ main ]
    paths:
      - 'argoproj/**'

jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout code
        uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Setup Kubernetes tools
        uses: yokawasa/action-setup-kube-tools@v0.9.2
        with:
          setup-tools: |
            kubeconform
            kustomize
            helm
          kubeconform: '0.6.4'
          kustomize: '5.3.0'
          helm: '3.14.2'

      - name: Get changed files
        id: changed-files
        run: |
          # PRの場合はPRで変更されたファイルを取得
          if [ "${{ github.event_name }}" == "pull_request" ]; then
            git fetch origin main
            git diff --name-only origin/main..HEAD > changed_files.txt
          else
            # pushの場合は前回のコミットとの差分を取得
            git diff --name-only ${{ github.event.before }} ${{ github.sha }} > changed_files.txt
          fi
          cat changed_files.txt

      - name: Identify changed directories
        id: changed-dirs
        run: |
          # 変更されたファイルを含むargoproj配下のディレクトリを特定
          CHANGED_DIRS=$(cat changed_files.txt | grep -E "^argoproj/" | xargs -I{} dirname {} | sort | uniq)
          echo "Changed directories:"
          echo "$CHANGED_DIRS"
          echo "changed_dirs=$CHANGED_DIRS" >> $GITHUB_OUTPUT

      - name: Validate manifests with Kubeconform
        run: |
          # 検証結果を保存するファイル
          VALIDATION_RESULTS="validation_results.md"
          echo "# Kubernetes マニフェスト検証結果" > $VALIDATION_RESULTS
          echo "" >> $VALIDATION_RESULTS
          
          # 変数の初期化
          TOTAL_VALID=0
          TOTAL_INVALID=0
          TOTAL_ERRORS=0
          
          # 各ディレクトリを順に処理
          for DIR in ${{ steps.changed-dirs.outputs.changed_dirs }}; do
            echo "## $DIR の検証" >> $VALIDATION_RESULTS
            echo "" >> $VALIDATION_RESULTS
            
            if [ -f "$DIR/kustomization.yaml" ]; then
              echo "### Kustomize ビルド検証" >> $VALIDATION_RESULTS
              echo "" >> $VALIDATION_RESULTS
              echo "```" >> $VALIDATION_RESULTS
              
              # kustomizeでビルドしてkubeconformで検証
              RESULT=$(kustomize build $DIR 2>&1 | kubeconform \
                -summary \
                -verbose \
                -schema-location default \
                -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' \
                -output json 2>&1 || true)
              
              echo "$RESULT" >> $VALIDATION_RESULTS
              echo "```" >> $VALIDATION_RESULTS
              
              # 統計を抽出
              VALID=$(echo "$RESULT" | grep -oP 'Valid: \K[0-9]+' || echo "0")
              INVALID=$(echo "$RESULT" | grep -oP 'Invalid: \K[0-9]+' || echo "0")
              ERRORS=$(echo "$RESULT" | grep -oP 'Errors: \K[0-9]+' || echo "0")
              
              TOTAL_VALID=$((TOTAL_VALID + VALID))
              TOTAL_INVALID=$((TOTAL_INVALID + INVALID))
              TOTAL_ERRORS=$((TOTAL_ERRORS + ERRORS))
              
            elif [ -f "$DIR/Chart.yaml" ]; then
              echo "### Helm チャート検証" >> $VALIDATION_RESULTS
              echo "" >> $VALIDATION_RESULTS
              echo "```" >> $VALIDATION_RESULTS
              
              # Helmチャートをlintで検証
              helm lint $DIR >> $VALIDATION_RESULTS 2>&1 || true
              
              echo "```" >> $VALIDATION_RESULTS
              
            else
              echo "### 個別YAMLファイル検証" >> $VALIDATION_RESULTS
              echo "" >> $VALIDATION_RESULTS
              echo "```" >> $VALIDATION_RESULTS
              
              # ディレクトリ内のYAMLファイルを検証
              YAML_FILES=$(find $DIR -name "*.yaml" -o -name "*.yml")
              if [ -n "$YAML_FILES" ]; then
                RESULT=$(echo "$YAML_FILES" | xargs kubeconform \
                  -summary \
                  -verbose \
                  -schema-location default \
                  -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' \
                  -output json 2>&1 || true)
                
                echo "$RESULT" >> $VALIDATION_RESULTS
                
                # 統計を抽出
                VALID=$(echo "$RESULT" | grep -oP 'Valid: \K[0-9]+' || echo "0")
                INVALID=$(echo "$RESULT" | grep -oP 'Invalid: \K[0-9]+' || echo "0")
                ERRORS=$(echo "$RESULT" | grep -oP 'Errors: \K[0-9]+' || echo "0")
                
                TOTAL_VALID=$((TOTAL_VALID + VALID))
                TOTAL_INVALID=$((TOTAL_INVALID + INVALID))
                TOTAL_ERRORS=$((TOTAL_ERRORS + ERRORS))
              else
                echo "YAMLファイルが見つかりませんでした。" >> $VALIDATION_RESULTS
              fi
              
              echo "```" >> $VALIDATION_RESULTS
            fi
            
            echo "" >> $VALIDATION_RESULTS
          done
          
          # 全体の集計を追加
          echo "## 検証結果サマリー" >> $VALIDATION_RESULTS
          echo "" >> $VALIDATION_RESULTS
          echo "- 有効なリソース: $TOTAL_VALID" >> $VALIDATION_RESULTS
          echo "- 無効なリソース: $TOTAL_INVALID" >> $VALIDATION_RESULTS
          echo "- エラー: $TOTAL_ERRORS" >> $VALIDATION_RESULTS
          echo "" >> $VALIDATION_RESULTS
          
          if [ $TOTAL_INVALID -gt 0 ] || [ $TOTAL_ERRORS -gt 0 ]; then
            echo "❌ 検証で問題が見つかりました。詳細は上記の結果を確認してください。" >> $VALIDATION_RESULTS
            exit 1
          else
            echo "✅ すべてのマニフェストが検証をパスしました。" >> $VALIDATION_RESULTS
          fi
          
          cat $VALIDATION_RESULTS

      - name: Comment PR with validation results
        if: github.event_name == 'pull_request'
        uses: actions/github-script@v7
        with:
          github-token: ${{ secrets.GITHUB_TOKEN }}
          script: |
            const fs = require('fs');
            const validationOutput = fs.readFileSync('validation_results.md', 'utf8');
            
            github.rest.issues.createComment({
              issue_number: context.issue.number,
              owner: context.repo.owner,
              repo: context.repo.repo,
              body: validationOutput
            });
```

### 3.2 使用するツールとバージョン

| ツール名 | バージョン | 用途 |
|----------|-----------|------|
| Kubeconform | 0.6.4 | マニフェスト検証 |
| Kustomize | 5.3.0 | マニフェストのビルド |
| Helm | 3.14.2 | Helmチャートの検証 |

### 3.3 検証フロー

1. PRまたはメインブランチへのプッシュで、argoproj配下のファイルに変更があった場合にワークフローを起動
2. 変更されたファイルを検出し、関連するディレクトリを特定
3. ディレクトリごとに適切な検証ツールを適用:
   - kustomization.yamlがある場合: Kustomizeでビルド後Kubeconformで検証
   - Chart.yamlがある場合: Helm lintで検証
   - それ以外: ディレクトリ内のYAMLファイルを直接Kubeconformで検証
4. 検証結果をMarkdownフォーマットで出力し、PRの場合はコメントとして投稿
5. エラーが見つかった場合はビルドを失敗させる

## 4. 技術的な選択肢と決定事項

### 4.1 検証ツールの選択

Kubeconformを主な検証ツールとして選定した理由:
- 高いパフォーマンス (複数のルーチンで並行処理)
- CRDのサポート
- JSON Schema形式でのスキーマ定義に対応
- 最新のKubernetesバージョンへの対応

### 4.2 CRDサポート

ArgoCDや他のカスタムリソースを適切に検証するために、datreeio/CRDs-catalogからスキーマを取得するオプションを追加:

```bash
-schema-location default -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'
```

これにより、以下のカスタムリソースの検証が可能になる:
- ArgoCDの`Application`リソース
- その他一般的なCRD

### 4.3 既存のargocd-diffワークフローとの関係

既存の`argocd-diff.yaml`ワークフローと新しい検証ワークフローは相互補完的に動作する:

- `argocd-diff.yaml`: 変更内容がクラスタにどのような影響を与えるかを視覚的に表示
- `validate-k8s-manifests.yaml`: マニフェストが構文的・意味的に正しいかを検証

両方のワークフローを並行して実行することで、より包括的な品質管理が可能になる。

## 5. メリットと期待される効果

1. マニフェストのエラーを早期発見できるようになり、デプロイ失敗の減少
2. 構文エラーや必須フィールドの欠落を事前に検出
3. PRレビュー時に、レビュアーが品質の確認に費やす時間の削減
4. 全体的なKubernetesマニフェストの品質向上
5. 本番環境での問題発生リスクの低減

## 6. 今後の拡張可能性

1. カスタムポリシーの適用 (Open Policy Agent等を使用)
2. 特定のloliceプロジェクト固有のルールの追加
3. 検証範囲の拡大（他のディレクトリのマニフェストなど）
4. 自動修正機能の追加

## 7. 実装手順

1. `validate-k8s-manifests.yaml`ファイルを作成
2. ワークフローのテスト
   - テスト用のPRで動作確認
   - 異なるタイプのマニフェスト（Kustomize, Helm, 単体YAML）でのテスト
3. 必要に応じてワークフローの調整
4. ドキュメントの更新
