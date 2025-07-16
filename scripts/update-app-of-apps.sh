#!/bin/bash
set -e

# 注意: kustomization.yamlは当初argoproj/argocd-apps/に配置予定だったが、
# kustomize v5.0.0以降のセキュリティポリシーにより、
# 親ディレクトリ（../）のファイル参照が制限されるため、
# argoproj/に配置してサブディレクトリのファイルを参照する方式に変更
KUSTOMIZATION_FILE="argoproj/kustomization.yaml"

echo "Updating App of Apps kustomization.yaml..."

# kustomization.yamlを生成
cat > $KUSTOMIZATION_FILE << 'EOF'
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

resources:
EOF

# application.yamlを検索して追加（argocd-apps自身は除外）
# argoproj/プレフィックスを削除してサブディレクトリからの相対パスに変換
find argoproj -name "application.yaml" -not -path "argoproj/argocd-apps/*" | \
  sort | \
  sed 's|^argoproj/||' | \
  sed 's/^/- /' >> $KUSTOMIZATION_FILE

echo "Updated $KUSTOMIZATION_FILE with $(grep -c '^-' $KUSTOMIZATION_FILE) applications"