#!/usr/bin/env bash
# 通常 CI (.github/workflows/chainsaw.yaml) が隔離 kind のノードで必要とする
# コンテナイメージを列挙する。出力は 1 行 1 イメージの TSV "<image>\t<scope>"。
#
#   scope=all    : 全ノードで必要 (CNI、DaemonSet)
#   scope=worker : worker ノードだけで必要 (Deployment / StatefulSet / 単発 Pod。
#                  kind は worker がある構成では control-plane に NoSchedule taint を
#                  付けるため、toleration の無い Pod は worker にしか乗らない)
#
# 列挙はマニフェスト (kustomize build / helm template / suite の fixture) から
# 動的に取り出す。ここで漏れた / 余分に挙げたイメージがあっても、kubelet が通常どおり
# pull するだけでテスト結果には影響しない (kind-prefetch-images.sh は best effort)。
#
# usage: REPO_ROOT=... kind-ci-images.sh '<components json>'
#   env CALICO_VERSION            : 指定時は Calico のイメージも列挙する (例 v3.33.0)
#   env TIGERA_OPERATOR_MANIFEST  : tigera-operator.yaml のローカルパス (operator イメージ取得用)
#   env MANIFEST_DIR              : kustomize build 済みの <component>.yaml があるディレクトリ。
#                                   あればそれを使い、無ければ kustomize build する
set -euo pipefail

REPO_ROOT="${REPO_ROOT:?REPO_ROOT is required}"
COMPONENTS_JSON="${1:?components json is required}"
CALICO_VERSION="${CALICO_VERSION:-}"
TIGERA_OPERATOR_MANIFEST="${TIGERA_OPERATOR_MANIFEST:-}"
MANIFEST_DIR="${MANIFEST_DIR:-}"

emit() { printf '%s\t%s\n' "$1" "$2"; }

# stdin のマニフェストから .image を集める。$1 は kind の条件 (yq 式)、$2 は scope。
manifest_images() {
  local kind_expr="$1" scope="$2" image
  yq -r "select(.kind ${kind_expr}) | .. | select(type == \"!!map\") | select(has(\"image\")) | .image" 2>/dev/null \
    | { grep -Ev '^(null|---|)$' || true; } | sort -u \
    | while read -r image; do emit "$image" "$scope"; done
}
# マニフェスト全体を DaemonSet (all) とそれ以外 (worker) に分けて列挙する。
split_by_daemonset() {
  local manifests
  manifests="$(cat)"
  manifest_images '== "DaemonSet"' all <<<"$manifests"
  manifest_images '!= "DaemonSet"' worker <<<"$manifests"
}
has_component() {
  python3 -c 'import json, sys; sys.exit(0 if sys.argv[1] in json.loads(sys.argv[2]) else 1)' "$1" "$COMPONENTS_JSON"
}
# component の kustomize 出力。ワークフローが先にビルドしたファイルがあればそれを使う
# (remote resource の再取得と、イメージ pull との帯域競合を避ける)。
component_manifests() {
  local component="$1" dir
  if [ -n "$MANIFEST_DIR" ] && [ -r "$MANIFEST_DIR/${component}.yaml" ]; then
    cat "$MANIFEST_DIR/${component}.yaml"
    return
  fi
  case "$component" in
    external-secrets-operator) dir=argoproj/external-secrets-operator/manifests/ ;;
    *) dir="argoproj/${component}/" ;;
  esac
  kustomize build "$dir" 2>/dev/null || true
}

cd "$REPO_ROOT"

{
  # Calico (tigera-operator 経由)。operator が展開するイメージは manifest に現れないため
  # 版固定の静的リスト (v3.33 で kind + Calico を起動したときに Pod が実際に使った 4 つ:
  # calico-node / typha / kube-controllers / apiserver / goldmane / whisker-backend は
  # 統合イメージ quay.io/calico/calico)。足りなければ kubelet が pull するだけ。
  if [ -n "$CALICO_VERSION" ]; then
    if [ -n "$TIGERA_OPERATOR_MANIFEST" ] && [ -r "$TIGERA_OPERATOR_MANIFEST" ]; then
      split_by_daemonset < "$TIGERA_OPERATOR_MANIFEST" | sed 's/\tworker$/\tall/'
    fi
    for name in calico node third-party-cni-plugins whisker; do
      emit "quay.io/calico/${name}:${CALICO_VERSION}" all
    done
  fi

  # ESO は argocd / external-secrets-operator の事前適用で Helm install される。
  if has_component external-secrets-operator || has_component argocd; then
    ESO_CHART_VERSION="$(yq -r '.spec.sources[] | select(.chart == "external-secrets") | .targetRevision' argoproj/external-secrets-operator/application.yaml)"
    { helm template external-secrets external-secrets \
        --repo https://charts.external-secrets.io --version "$ESO_CHART_VERSION" \
        --namespace external-secrets 2>/dev/null || true; } | split_by_daemonset
  fi

  for component in $(python3 -c 'import json, sys; print("\n".join(json.loads(sys.argv[1])))' "$COMPONENTS_JSON"); do
    component_manifests "$component" | split_by_daemonset
    case "$component" in
      grafana-alloy)
        # Alloy 本体は suite (log-pipeline.sh) が Helm install する DaemonSet。
        chart_repo="$(yq -r '.spec.sources[] | select(.chart == "alloy") | .repoURL' argoproj/grafana-alloy/application.yaml)"
        chart_version="$(yq -r '.spec.sources[] | select(.chart == "alloy") | .targetRevision' argoproj/grafana-alloy/application.yaml)"
        { helm template grafana-alloy alloy --repo "$chart_repo" --version "$chart_version" \
            --namespace monitoring --values argoproj/grafana-alloy/helm/values.yaml 2>/dev/null || true; } | split_by_daemonset
        ;;
    esac
    # suite が作る fixture / probe Pod (loki, curl, python など)。
    if [ -d "tests/chainsaw/${component}" ]; then
      { grep -rhoE '^\s*(- )?image:\s*[^[:space:]"'"'"']+' "tests/chainsaw/${component}/" || true; } \
        | sed -E 's/^\s*(- )?image:\s*//' | sort -u \
        | while read -r image; do emit "$image" worker; done
    fi
  done
} | awk -F'\t' '!seen[$1]++ || ($2 == "all" && scope[$1] != "all") { scope[$1] = $2 } END { for (i in scope) printf "%s\t%s\n", i, scope[i] }' | sort
