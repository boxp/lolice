#!/usr/bin/env bash
# BOXP-619: Calico 更新を「Typha 保留 → calico-node 段階更新 → 保留解除」の順で隔離 kind 上に再現し、
# 更新特有の安全性を検査する。fresh install の合格では代替できない次の性質を見る。
#   - prune_safety   : base の Argo desired state にあった CRD が head で消えるなら automated.prune が false
#   - install_base   : base (main) の application.yaml / values.yaml を Argo と同じ helm template で SSA 適用
#   - hold_and_upgrade: Typha に unsupported.operator.tigera.io/ignore=true を付けてから head を適用し、
#                      calico-node が head 版へ入れ替わる間 Typha が base 版のまま (Pod 不変) で、
#                      calico-node の同時 not-ready が 1 台以下であること
#   - release_hold   : annotation を外すと Typha が head 版になり、全 tigerastatus が Available に戻ること
#   - post_checks    : 更新中の連続疎通 (ノード跨ぎ / DNS) の失敗率、DNS、NetworkPolicy の enforce、CSI driver
# 使い方: upgrade.sh <section>。base は CALICO_BASE_DIR (argoproj/calico を含む base 版の lolice root)、
# head は REPO_ROOT。状態は CALICO_UPGRADE_STATE_DIR に残す。
set -euo pipefail

SECTION="${1:?usage: upgrade.sh <section>}"
: "${REPO_ROOT:?REPO_ROOT is required}"
"${REPO_ROOT}/tests/workflows/require-isolated-kind.sh" --loopback

SUITE_DIR="${REPO_ROOT}/tests/chainsaw/calico-upgrade"
STATE_DIR="${CALICO_UPGRADE_STATE_DIR:-${RUNNER_TEMP:-/tmp}/calico-upgrade-state}"
mkdir -p "$STATE_DIR"
HOLD_ANNOTATION='unsupported.operator.tigera.io/ignore'
PROBE_NS=calico-upgrade-probe
# 更新中の連続疎通で許容する失敗率 (%)。calico-node 1 台の再起動中の瞬断だけを許し、
# 全ノード同時の切断 (保留なしの既定経路で起きうる) は超えるようにする。
MAX_FAIL_PERCENT="${CALICO_UPGRADE_MAX_FAIL_PERCENT:-5}"

log() { printf '%s %s\n' "$(date -u +%H:%M:%S)" "$*"; }
fail() { log "FAIL: $*" >&2; exit 1; }

app_file() { echo "$1/argoproj/calico/application.yaml"; }

# tigera-operator source の chart 版 (v 付きに正規化)。operator が出す calico-node / typha の tag と一致するはず。
chart_version() {
  local v
  v="$(yq -r '.spec.sources[] | select(.chart == "tigera-operator") | .targetRevision' "$(app_file "$1")")"
  [[ "$v" =~ ^v?[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "tigera-operator targetRevision must be a fixed version: $v"
  echo "v${v#v}"
}

# Argo CD と同じく application.yaml の chart source ごとに helm template --include-crds で描画する。
# $lolice/ の valueFiles はその版の lolice root から読む。helm hook (pre-delete の uninstall Job) は
# Argo の sync 対象ではないので除く。kind 用の差分は 1 点だけ: 本番はノードアドレスを
# 192.168.10.0/24 で自動検出するが、kind のノードは docker network 上にいるため NodeInternalIP を使う。
render() {
  local root="$1" app i n chart repo ver vf
  app="$(app_file "$root")"
  n="$(yq '.spec.sources | length' "$app")"
  for i in $(seq 0 $((n - 1))); do
    chart="$(yq -r ".spec.sources[$i].chart // \"\"" "$app")"
    [ -n "$chart" ] || continue
    repo="$(yq -r ".spec.sources[$i].repoURL" "$app")"
    ver="$(yq -r ".spec.sources[$i].targetRevision" "$app")"
    local args=()
    while IFS= read -r vf; do
      [ -n "$vf" ] || continue
      case "$vf" in
        '$lolice/'*) args+=(-f "$root/${vf#\$lolice/}") ;;
        *) fail "unsupported valueFile: $vf" ;;
      esac
    done < <(yq -r ".spec.sources[$i].helm.valueFiles[]?" "$app")
    helm template calico "$chart" --repo "$repo" --version "$ver" \
      --namespace tigera-operator --include-crds "${args[@]}"
    echo '---'
  done | yq 'select(. != null and .metadata.annotations["helm.sh/hook"] == null)
    | (select(.kind == "Installation") | .spec.calicoNetwork.nodeAddressAutodetectionV4) = {"kubernetes": "NodeInternalIP"}'
}

crd_names() { yq -r 'select(.kind == "CustomResourceDefinition") | .metadata.name' "$1" | grep -v '^---$' | sort -u; }

# Argo の ServerSideApply=true と同じく SSA で適用する (prune はしない)。CRD を先に Established にする。
apply_render() {
  local file="$1"
  kubectl create namespace tigera-operator --dry-run=client -o yaml | kubectl apply -f -
  yq 'select(.kind == "CustomResourceDefinition")' "$file" \
    | kubectl apply --server-side --force-conflicts --field-manager=argocd-controller -f -
  kubectl wait --for=condition=established --timeout=120s $(crd_names "$file" | sed 's|^|crd/|')
  yq 'select(.kind != "CustomResourceDefinition")' "$file" \
    | kubectl apply --server-side --force-conflicts --field-manager=argocd-controller -f -
}

image_tag() { sed 's/.*://'; }
node_image() { kubectl get ds calico-node -n calico-system -o jsonpath='{.spec.template.spec.containers[?(@.name=="calico-node")].image}' 2>/dev/null | image_tag; }
typha_image() { kubectl get deploy calico-typha -n calico-system -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null | image_tag; }

# Ready かつ削除中でない calico-node Pod の数と、そのうち指定 tag の数。
node_counts() {
  kubectl get pods -n calico-system -l k8s-app=calico-node -o json | jq -r --arg tag "$1" '
    [.items[] | select(.metadata.deletionTimestamp == null)
      | select(any(.status.conditions[]?; .type == "Ready" and .status == "True"))] as $ready
    | "\($ready | length) \([$ready[] | select(.spec.containers[] | select(.name == "calico-node") | .image | endswith(":" + $tag))] | length)"'
}
desired_nodes() { kubectl get nodes -o name | wc -l; }

typha_ready() {
  local want="$1" st
  st="$(kubectl get deploy calico-typha -n calico-system -o json)"
  jq -e --arg tag "$want" '
    (.spec.template.spec.containers[0].image | endswith(":" + $tag))
    and .status.observedGeneration == .metadata.generation
    and (.status.updatedReplicas // 0) == .spec.replicas
    and (.status.readyReplicas // 0) == .spec.replicas
    and (.status.replicas // 0) == .spec.replicas' <<<"$st" >/dev/null
}

all_tigerastatus_available() {
  kubectl get tigerastatus -o json | jq -e '
    (.items | length) > 0 and all(.items[];
      any(.status.conditions[]?; .type == "Available" and .status == "True")
      and all(.status.conditions[]?; .type != "Degraded" or .status != "True"))' >/dev/null
}

wait_steady() {
  local want="$1" timeout="$2" deadline counts
  deadline=$((SECONDS + timeout))
  while :; do
    counts="$(node_counts "$want" 2>/dev/null || echo '0 0')"
    if [ "$(node_image)" = "$want" ] && [ "$counts" = "$(desired_nodes) $(desired_nodes)" ] \
       && typha_ready "$want" 2>/dev/null && all_tigerastatus_available 2>/dev/null \
       && kubectl wait --for=condition=Ready node --all --timeout=1s >/dev/null 2>&1; then
      log "Calico $want steady: calico-node $counts, typha $(typha_image)"
      return 0
    fi
    [ "$SECONDS" -lt "$deadline" ] || {
      kubectl get tigerastatus -o wide || true
      kubectl get pods -n calico-system -o wide || true
      fail "Calico $want が ${timeout}s 以内に steady になりません (node image=$(node_image), counts=$counts, typha=$(typha_image))"
    }
    sleep 5
  done
}

typha_pod_uids() { kubectl get pods -n calico-system -l k8s-app=calico-typha -o jsonpath='{range .items[*]}{.metadata.uid}{"\n"}{end}' | sort; }

case "$SECTION" in
  prune_safety)
    : "${CALICO_BASE_DIR:?CALICO_BASE_DIR is required}"
    render "$CALICO_BASE_DIR" > "$STATE_DIR/base.yaml"
    render "$REPO_ROOT" > "$STATE_DIR/head.yaml"
    # Calico の版は chart ではなく operator の版で決まる。values で operator 版を固定したまま
    # chart だけ上げると calico-node / typha は更新されないので、chart が組む operator 版と一致させる。
    app="$(app_file "$REPO_ROOT")"
    paired="$(helm show values tigera-operator \
      --repo "$(yq -r '.spec.sources[] | select(.chart == "tigera-operator") | .repoURL' "$app")" \
      --version "$(yq -r '.spec.sources[] | select(.chart == "tigera-operator") | .targetRevision' "$app")" \
      | yq -r '.tigeraOperator.version')"
    rendered="$(yq -r 'select(.kind == "Deployment" and .metadata.name == "tigera-operator") | .spec.template.spec.containers[0].image' "$STATE_DIR/head.yaml" | image_tag)"
    log "head operator image tag=$rendered (chart $(chart_version "$REPO_ROOT") の既定 $paired)"
    [ "$rendered" = "$paired" ] || fail "operator 版 $rendered が chart の組む $paired と一致しません"
    removed="$(comm -23 <(crd_names "$STATE_DIR/base.yaml") <(crd_names "$STATE_DIR/head.yaml"))"
    prune="$(yq -r '.spec.syncPolicy.automated.prune // false' "$(app_file "$REPO_ROOT")")"
    log "base CRD $(crd_names "$STATE_DIR/base.yaml" | wc -l) / head CRD $(crd_names "$STATE_DIR/head.yaml" | wc -l), automated.prune=$prune"
    [ -z "$removed" ] && { log "head で消える CRD なし"; exit 0; }
    printf 'head の desired state から外れる CRD:\n%s\n' "$removed"
    # 3.32 で chart から CRD が外れた時に prune=true のままだと Argo が全 CRD を消してネットワークが止まる。
    [ "$prune" = false ] || fail "CRD が desired state から外れるのに automated.prune=true (CD-02)"
    # Calico 本体が使う projectcalico.org / operator.tigera.io の CRD は head に残っていなければならない。
    if grep -E '\.(crd\.projectcalico\.org|operator\.tigera\.io)$' <<<"$removed"; then
      fail "Calico が使う CRD が head の desired state から外れています"
    fi
    ;;

  install_base)
    : "${CALICO_BASE_DIR:?CALICO_BASE_DIR is required}"
    [ -s "$STATE_DIR/base.yaml" ] || render "$CALICO_BASE_DIR" > "$STATE_DIR/base.yaml"
    apply_render "$STATE_DIR/base.yaml"
    wait_steady "$(chart_version "$CALICO_BASE_DIR")" 900
    ;;

  deploy_probes)
    kubectl apply -f "$SUITE_DIR/probes.yaml"
    kubectl rollout status ds/probe-server -n "$PROBE_NS" --timeout=300s
    kubectl rollout status ds/probe-client -n "$PROBE_NS" --timeout=300s
    sleep 10
    ;;

  hold_and_upgrade)
    : "${CALICO_BASE_DIR:?CALICO_BASE_DIR is required}"
    base="$(chart_version "$CALICO_BASE_DIR")"
    head="$(chart_version "$REPO_ROOT")"
    [ -s "$STATE_DIR/head.yaml" ] || render "$REPO_ROOT" > "$STATE_DIR/head.yaml"
    date +%s > "$STATE_DIR/window-start"
    kubectl annotate deployment calico-typha -n calico-system "${HOLD_ANNOTATION}=true" --overwrite
    typha_pod_uids > "$STATE_DIR/typha-uids-before"
    typha_gen="$(kubectl get deploy calico-typha -n calico-system -o jsonpath='{.metadata.generation}')"
    log "Typha を保留 ($base) して head ($head) を適用"
    apply_render "$STATE_DIR/head.yaml"
    desired="$(desired_nodes)"
    max_unready=0
    deadline=$((SECONDS + 1200))
    while :; do
      read -r ready updated < <(node_counts "$head")
      unready=$((desired - ready))
      [ "$unready" -le "$max_unready" ] || { max_unready=$unready; log "calico-node not-ready=$unready (ready=$ready updated=$updated)"; }
      [ "$(typha_image)" = "$base" ] || fail "保留中に Typha が $(typha_image) に変わりました"
      [ "$(kubectl get deploy calico-typha -n calico-system -o jsonpath='{.metadata.generation}')" = "$typha_gen" ] \
        || fail "保留中に Typha Deployment の spec が変更されました"
      if [ "$(node_image)" = "$head" ] && [ "$updated" = "$desired" ] && [ "$ready" = "$desired" ]; then
        break
      fi
      [ "$SECONDS" -lt "$deadline" ] || {
        kubectl get pods -n calico-system -o wide || true
        fail "calico-node が $head に揃いません (node image=$(node_image), ready=$ready, updated=$updated)"
      }
      sleep 2
    done
    echo "$max_unready" > "$STATE_DIR/max-unready-hold"
    diff "$STATE_DIR/typha-uids-before" <(typha_pod_uids) || fail "保留中に Typha Pod が入れ替わりました"
    log "calico-node $desired/$desired が $head、Typha は $base のまま (Pod 不変)。同時 not-ready 最大 $max_unready"
    [ "$max_unready" -le 1 ] || fail "calico-node の同時 not-ready が $max_unready 台 (許容 1 台)"
    ;;

  release_hold)
    head="$(chart_version "$REPO_ROOT")"
    kubectl annotate deployment calico-typha -n calico-system "${HOLD_ANNOTATION}-"
    desired="$(desired_nodes)"
    max_unready=0
    deadline=$((SECONDS + 900))
    while ! typha_ready "$head" 2>/dev/null; do
      read -r ready _ < <(node_counts "$head")
      unready=$((desired - ready))
      [ "$unready" -le "$max_unready" ] || { max_unready=$unready; log "Typha 更新中 calico-node not-ready=$unready"; }
      [ "$SECONDS" -lt "$deadline" ] || fail "保留解除後に Typha が $head になりません (typha=$(typha_image))"
      sleep 2
    done
    log "Typha が $head に更新 (更新中の calico-node 同時 not-ready 最大 $max_unready)"
    [ "$max_unready" -le 1 ] || fail "Typha 更新中に calico-node の同時 not-ready が $max_unready 台"
    wait_steady "$head" 600
    [ -z "$(kubectl get deploy calico-typha -n calico-system -o jsonpath="{.metadata.annotations.unsupported\.operator\.tigera\.io/ignore}")" ] \
      || fail "保留 annotation が残っています"
    date +%s > "$STATE_DIR/window-end"
    ;;

  post_checks)
    start="$(cat "$STATE_DIR/window-start")"
    end="$(cat "$STATE_DIR/window-end")"
    total=0; failed=0
    for pod in $(kubectl get pods -n "$PROBE_NS" -l app=probe-client -o name); do
      stats="$(kubectl logs -n "$PROBE_NS" "$pod" | awk -v s="$start" -v e="$end" '
        $1 >= s && $1 <= e { n++; if ($2 != "OK") f++ } END { printf "%d %d\n", n, f }')"
      read -r n f <<<"$stats"
      log "$pod: 更新中の probe $n 件、失敗 $f 件"
      [ "$n" -gt 0 ] || fail "$pod の更新中の probe 記録がありません"
      total=$((total + n)); failed=$((failed + f))
    done
    log "更新中の連続疎通: $total 件中 失敗 $failed 件 (許容 ${MAX_FAIL_PERCENT}%)"
    [ $((failed * 100)) -le $((total * MAX_FAIL_PERCENT)) ] || fail "更新中の疎通失敗率が許容値を超えました"

    servers="$(kubectl get pods -n "$PROBE_NS" -l app=probe-server -o jsonpath='{range .items[*]}{.status.podIP}{"\n"}{end}')"
    clients="$(kubectl get pods -n "$PROBE_NS" -l app=probe-client -o name)"
    probe_all() { # $1=expect ok|blocked
      local c ip
      for c in $clients; do
        for ip in $servers; do
          if kubectl exec -n "$PROBE_NS" "$c" -- wget -q -T 3 -O /dev/null "http://$ip:8080/hostname"; then
            [ "$1" = ok ] || fail "NetworkPolicy deny 中に $c -> $ip が通りました"
          else
            [ "$1" = blocked ] || fail "$c -> $ip が通りません"
          fi
        done
      done
    }
    probe_all ok
    for c in $clients; do
      kubectl exec -n "$PROBE_NS" "$c" -- nslookup kubernetes.default.svc.cluster.local >/dev/null \
        || fail "$c から DNS 解決できません"
    done
    log "更新後のノード跨ぎ疎通と DNS: OK"

    kubectl apply -f - <<EOF
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: deny-probe-server
  namespace: $PROBE_NS
spec:
  podSelector:
    matchLabels:
      app: probe-server
  policyTypes: [Ingress]
EOF
    sleep 5
    probe_all blocked
    kubectl apply -f - <<EOF
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-probe-client
  namespace: $PROBE_NS
spec:
  podSelector:
    matchLabels:
      app: probe-server
  policyTypes: [Ingress]
  ingress:
    - from:
        - podSelector:
            matchLabels:
              app: probe-client
      ports:
        - port: 8080
EOF
    sleep 5
    probe_all ok
    log "NetworkPolicy の deny / allow が更新後の Calico で enforce されています"

    kubectl rollout status ds/csi-node-driver -n calico-system --timeout=120s \
      || fail "Calico CSI driver (kubeletVolumePluginPath) が Ready ではありません"
    ;;

  diagnostics)
    set +e
    kubectl get tigerastatus -o wide
    kubectl get installation default -o yaml | yq '.status'
    kubectl get deploy,ds,pods -n calico-system -o wide
    kubectl get deploy calico-typha -n calico-system -o yaml | yq '.metadata.annotations'
    kubectl logs -n tigera-operator deploy/tigera-operator --tail=80
    kubectl get pods -n "$PROBE_NS" -o wide
    exit 0
    ;;

  *)
    fail "unknown section: $SECTION"
    ;;
esac
