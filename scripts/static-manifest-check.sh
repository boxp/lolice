#!/usr/bin/env bash
# 静的マニフェスト検証 (BOXP-266)
#
# app-of-apps (argoproj/kustomization.yaml) を render して Argo CD Application を列挙し、
# この repo 内を source にする path を kustomize build / directory 収集で render したうえで
# conftest (Rego, policy/) にかける。kind や本番クラスタには一切触れない。
# chainsaw (kind 統合テスト) より前に数十秒で終わる静的ゲートとして CI と手元の両方で使う。
#
# 使い方: scripts/static-manifest-check.sh            (render 先は mktemp)
#         OUT_DIR=/tmp/rendered scripts/static-manifest-check.sh
# 必要なツール: kustomize, yq (mikefarah v4), conftest
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${OUT_DIR:-$(mktemp -d)}"
cd "$REPO_ROOT"

for tool in kustomize yq conftest; do
  command -v "$tool" >/dev/null 2>&1 || { echo "必要なツールがありません: $tool" >&2; exit 2; }
done

mkdir -p "$OUT_DIR/rendered"
started=$SECONDS

# 1. app-of-apps を render して Application 一覧を得る (chart 固定版などの policy もこのファイルに当てる)
kustomize build argoproj/ > "$OUT_DIR/applications.yaml"
app_count="$(yq -N 'select(.kind == "Application") | .metadata.name' "$OUT_DIR/applications.yaml" | wc -l)"
echo "applications: $app_count (argoproj/kustomization.yaml)"

# 2. この repo を source にする path を列挙する (単一 source / multi-source の両方)。
#    repoURL は https://github.com/boxp/lolice(.git) と git@github.com:boxp/lolice.git の両方に一致させる。
#    形式: "<path> <recurse: true|false>"
yq -N '
  select(.kind == "Application")
  | .spec
  | ([.source] + (.sources // []))
  | .[]
  | select(. != null)
  | select((.repoURL // "") | test("github\.com[:/]boxp/lolice"))
  | select(.path != null)
  | [.path, ((.directory.recurse // false) | tostring)]
  | join(" ")
' "$OUT_DIR/applications.yaml" | sort -u > "$OUT_DIR/sources.txt"

render_failures=0
while read -r src_path recurse; do
  [ -n "$src_path" ] || continue
  if [ ! -d "$src_path" ]; then
    echo "::error::Application の source path が存在しません: $src_path"
    render_failures=$((render_failures + 1))
    continue
  fi
  slug="$(echo "$src_path" | tr '/' '_')"
  out="$OUT_DIR/rendered/$slug.yaml"
  if [ -f "$src_path/kustomization.yaml" ] || [ -f "$src_path/kustomization.yml" ] || [ -f "$src_path/Kustomization" ]; then
    if ! kustomize build "$src_path" > "$out" 2> "$out.err"; then
      echo "::error::kustomize build に失敗: $src_path"
      cat "$out.err" >&2
      render_failures=$((render_failures + 1))
    fi
  else
    # Argo CD の directory source 相当: *.yaml / *.yml / *.json を集める (recurse=false なら直下のみ)
    depth_args=(-maxdepth 1)
    [ "$recurse" = "true" ] && depth_args=()
    : > "$out"
    while IFS= read -r f; do
      printf -- '---\n' >> "$out"
      cat "$f" >> "$out"
      printf '\n' >> "$out"
    done < <(find "$src_path" "${depth_args[@]}" -type f \( -name '*.yaml' -o -name '*.yml' -o -name '*.json' \) | sort)
  fi
  rm -f "$out.err"
done < "$OUT_DIR/sources.txt"

echo "rendered: $(wc -l < "$OUT_DIR/sources.txt") source paths -> $OUT_DIR/rendered ($((SECONDS - started))s)"
if [ "$render_failures" -ne 0 ]; then
  echo "::error::render に失敗した source が $render_failures 件あります"
  exit 1
fi

# 3. Rego policy (conftest)。render 済みマニフェストと Application、kustomization.yaml を別 policy で検査する。
policy_started=$SECONDS
mapfile -t kustomizations < <(git ls-files 'argoproj/kustomization.yaml' 'argoproj/**/kustomization.yaml')
conftest test --no-color -p policy/kubernetes "$OUT_DIR/applications.yaml" "$OUT_DIR"/rendered/*.yaml
conftest test --no-color -p policy/kustomize "${kustomizations[@]}"
for manifest in "$OUT_DIR"/rendered/*.yaml; do
  conftest test --no-color --combine -p policy/references "$manifest"
done
echo "policy: ok ($((SECONDS - policy_started))s)"
echo "static-manifest-check: ok (total $((SECONDS - started))s)"
