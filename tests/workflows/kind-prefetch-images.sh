#!/usr/bin/env bash
# kind ノード内の containerd (k8s.io namespace) へコンテナイメージを先行 pull する。
#
# 通常 CI では kind 作成直後にバックグラウンドで起動し、Calico の起動や CRD / マニフェストの
# 適用と並行してイメージ取得を済ませる。Pod が作られたときには kubelet が
# "Container image already present on machine" で即起動できる。
#
# best effort: どのイメージが取れなくても exit 0 で終わる (kubelet が通常どおり pull する)。
# レジストリの一時的な 429 などは数回リトライする。
#
# usage: kind-prefetch-images.sh <kind cluster name> < images.tsv   (TSV: "<image>\t<all|worker>")
#   env PREFETCH_PARALLEL    : 同時 pull 数 (既定 6)
#   env PREFETCH_RETRY_SLEEP : リトライ間隔の基数秒 (既定 5)
set -uo pipefail

normalize_ref() {
  # containerd (ctr) は docker.io/library/ の補完をしないので完全修飾名にする。
  local ref="$1" first
  first="${ref%%/*}"
  if [[ "$ref" != */* ]]; then
    printf 'docker.io/library/%s\n' "$ref"
  elif [[ "$first" == *.* || "$first" == *:* || "$first" == localhost ]]; then
    printf '%s\n' "$ref"
  else
    printf 'docker.io/%s\n' "$ref"
  fi
}

pull_one() {
  local node="$1" image="$2" attempt err started=$SECONDS
  for attempt in 1 2 3; do
    if err="$(docker exec "$node" ctr -n k8s.io images pull --hosts-dir /etc/containerd/certs.d "$image" 2>&1 >/dev/null)"; then
      printf 'ok   %-28s %3ds  %s\n' "$node" "$((SECONDS - started))" "$image"
      return 0
    fi
    sleep $((attempt * ${PREFETCH_RETRY_SLEEP:-5}))
  done
  printf 'fail %-28s %3ds  %s: %s\n' "$node" "$((SECONDS - started))" "$image" "$(tail -n 1 <<<"$err")"
  return 1
}

# スクリプトとして実行されたときだけ本体を動かす (source してテストできるように)。
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  CLUSTER="${1:?kind cluster name is required}"
  PARALLEL="${PREFETCH_PARALLEL:-6}"
  export -f pull_one

  all_nodes=(); worker_nodes=()
  while IFS=$'\t' read -r name role; do
    [ -n "$name" ] || continue
    all_nodes+=("$name")
    [ "$role" = worker ] && worker_nodes+=("$name")
  done < <(docker ps --filter "label=io.x-k8s.kind.cluster=${CLUSTER}" --format '{{.Names}}\t{{.Label "io.x-k8s.kind.role"}}')
  if [ "${#all_nodes[@]}" -eq 0 ]; then
    echo "kind cluster '${CLUSTER}' のノードが見つかりません。先行 pull をスキップします。" >&2
    exit 0
  fi
  # worker の無い 1 ノード構成では全ノード (= control-plane) が対象。
  [ "${#worker_nodes[@]}" -gt 0 ] || worker_nodes=("${all_nodes[@]}")

  started=$SECONDS
  jobs_file="$(mktemp)"
  while IFS=$'\t' read -r image scope; do
    [ -n "$image" ] || continue
    ref="$(normalize_ref "$image")"
    case "$scope" in
      all) targets=("${all_nodes[@]}") ;;
      *) targets=("${worker_nodes[@]}") ;;
    esac
    for node in "${targets[@]}"; do printf '%s\t%s\n' "$node" "$ref"; done
  done | sort -u > "$jobs_file"

  echo "prefetch: $(wc -l < "$jobs_file") pulls across ${#all_nodes[@]} node(s), parallel=${PARALLEL}"
  xargs -r -P "$PARALLEL" -L 1 bash -c 'pull_one "$0" "$1"' < "$jobs_file" | tee "${jobs_file}.result"
  ok="$(grep -c '^ok ' "${jobs_file}.result" || true)"
  failed="$(grep -c '^fail ' "${jobs_file}.result" || true)"
  echo "prefetch: done in $((SECONDS - started))s (ok=${ok} fail=${failed})"
  rm -f "$jobs_file" "${jobs_file}.result"
  exit 0
fi
