#!/usr/bin/env bash
# 隔離 kind 上の Langfuse に対する動的検査。section ごとに chainsaw の step から呼ぶ。
set -euo pipefail
: "${REPO_ROOT:?REPO_ROOT is required}"
: "${CHAINSAW_KIND_KUBECONFIG:?An isolated kubeconfig is required}"
expected_server="$(kubectl --kubeconfig "$CHAINSAW_KIND_KUBECONFIG" config view --minify -o jsonpath='{.clusters[0].cluster.server}')"
test "$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')" = "$expected_server"

SUITE="$REPO_ROOT/tests/chainsaw/langfuse"
NS=langfuse

diagnose() {
  kubectl get pods -n "$NS" -o wide || true
  kubectl get externalsecret -n "$NS" || true
  kubectl describe pods -n "$NS" | tail -n 60 || true
  kubectl get events -n "$NS" --sort-by=.lastTimestamp | tail -n 30 || true
}

# workflow は argocd / external-secrets-operator 選択時だけ ESO を入れるので、無ければここで本番と同じ版を入れる
# (grafana-alloy suite が Alloy を自前で Helm install するのと同じ形)。
ensure_eso() {
  if ! kubectl get deployment external-secrets -n external-secrets >/dev/null 2>&1; then
    local version
    version="$(yq -r '.spec.sources[] | select(.chart == "external-secrets") | .targetRevision' "$REPO_ROOT/argoproj/external-secrets-operator/application.yaml")"
    [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo 'ESO targetRevision must be a fixed chart version.' >&2; exit 1; }
    helm upgrade --install external-secrets external-secrets --repo https://charts.external-secrets.io \
      --version "$version" --namespace external-secrets --create-namespace --timeout 180s >/dev/null
  fi
  kubectl wait deployment --all -n external-secrets --for=condition=Available --timeout=240s
}

deploy() {
  local manifest attempt
  ensure_eso
  manifest="$(mktemp)"
  "$SUITE/render-fixture.sh" > "$manifest"
  # ESO webhook の CA 注入が追いつくまで短く再試行する (workflow と同じ)。
  for attempt in $(seq 1 12); do
    kubectl apply --server-side --force-conflicts -f "$manifest" >/dev/null && break
    [ "$attempt" -lt 12 ] || { echo "manifest の適用に失敗しました" >&2; exit 1; }
    sleep 5
  done
  rm -f "$manifest"
  kubectl wait -n "$NS" --for=condition=Ready externalsecret/langfuse-secrets externalsecret/langfuse-ingest-key --timeout=120s \
    || { diagnose; exit 1; }
  deploy_oidc_fixture
  kubectl wait -n "$NS" --for=condition=Available deployment --all --timeout=420s || { diagnose; exit 1; }
  local sts
  for sts in langfuse-postgres langfuse-clickhouse langfuse-minio; do
    kubectl rollout status -n "$NS" "statefulset/$sts" --timeout=120s || { diagnose; exit 1; }
  done
  # 観測基盤はどの Pod も先取りせず、最低優先度で動く。
  kubectl get pods -n "$NS" -l '!chainsaw-probe' -o json | jq -e '[.items[] | select(.status.phase == "Running")] as $p
    | ($p | length) >= 6 and ($p | all(.spec.priority == -100 and .spec.preemptionPolicy == "Never"))' >/dev/null \
    || { echo 'PriorityClass が適用されていません' >&2; exit 1; }
}

secret_value() {
  kubectl get secret "$1" -n "$NS" -o json | jq -er --arg key "$2" '.data[$key] | @base64d'
}

secrets_and_rbac() {
  # ESO generator が設計どおりの値を生成し、値が互いに独立である。
  secret_value langfuse-ingest-key LANGFUSE_PUBLIC_KEY | grep -Eq '^pk-lf-[A-Za-z0-9]{48}$'
  secret_value langfuse-ingest-key LANGFUSE_SECRET_KEY | grep -Eq '^sk-lf-[A-Za-z0-9]{48}$'
  secret_value langfuse-secrets ENCRYPTION_KEY | grep -Eq '^[0-9a-f]{64}$'
  kubectl get secret langfuse-secrets -n "$NS" -o json | jq -e '
    [.data | to_entries[] | select(.key | IN("NEXTAUTH_SECRET", "SALT", "POSTGRES_PASSWORD", "CLICKHOUSE_PASSWORD", "REDIS_AUTH", "MINIO_ROOT_PASSWORD", "LANGFUSE_INIT_USER_PASSWORD")) | .value]
    | length == 7 and (unique | length == 7)' >/dev/null
  test "$(secret_value langfuse-ingest-key LANGFUSE_PUBLIC_KEY | cut -c7-)" != "$(secret_value langfuse-ingest-key LANGFUSE_SECRET_KEY | cut -c7-)"

  # producer の ServiceAccount は ingest key 1 件だけを get できる。
  local sa answer verb_resource
  for sa in hermes-agent codex-workspace; do
    answer="$(kubectl auth can-i get secret/langfuse-ingest-key -n "$NS" --as="system:serviceaccount:$sa:langfuse-ingest-key-reader")"
    test "$answer" = yes
    for verb_resource in "get secret/langfuse-secrets" "list secrets" "watch secrets" "get secret/langfuse-cloudflared" "update secret/langfuse-ingest-key"; do
      # shellcheck disable=SC2086
      answer="$(kubectl auth can-i $verb_resource -n "$NS" --as="system:serviceaccount:$sa:langfuse-ingest-key-reader" || true)"
      test "$answer" = no || { echo "$sa に過剰な権限: $verb_resource" >&2; exit 1; }
    done
  done
  answer="$(kubectl auth can-i get secret/langfuse-ingest-key -n "$NS" --as=system:serviceaccount:default:default || true)"
  test "$answer" = no
}

# Cloudflare Access の代わりの合成 issuer。ID token の iss/aud/署名/期限を変えられる。
deploy_oidc_fixture() {
  kubectl create configmap langfuse-oidc-fixture -n "$NS" --dry-run=client -o yaml \
    --from-file=fixture.py="$SUITE/oidc-fixture.py" --from-file=client.py="$SUITE/oidc-client.py" | kubectl apply -f - >/dev/null
  cat <<'YAML' | kubectl apply -f - >/dev/null
apiVersion: v1
kind: Service
metadata:
  name: langfuse-oidc-fixture
  namespace: langfuse
spec:
  selector:
    app: langfuse-oidc-fixture
  ports:
    - port: 8080
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: langfuse-oidc-fixture
  namespace: langfuse
spec:
  replicas: 1
  selector:
    matchLabels:
      app: langfuse-oidc-fixture
  template:
    metadata:
      labels:
        app: langfuse-oidc-fixture
        chainsaw-probe: "true"
    spec:
      priorityClassName: langfuse-observability
      terminationGracePeriodSeconds: 1
      automountServiceAccountToken: false
      containers:
        - name: fixture
          image: python:3.13-alpine
          command: ["python", "/app/fixture.py"]
          envFrom:
            - secretRef:
                name: langfuse-oidc
          readinessProbe:
            httpGet:
              path: /healthz
              port: 8080
          volumeMounts:
            - name: app
              mountPath: /app
      volumes:
        - name: app
          configMap:
            name: langfuse-oidc-fixture
YAML
}

oidc_client() {
  kubectl exec -n "$NS" deployment/langfuse-oidc-fixture -- \
    env "OWNER_PASSWORD=$(secret_value langfuse-secrets LANGFUSE_INIT_USER_PASSWORD)" python /app/client.py "$@"
}

sql() { kubectl exec -n "$NS" statefulset/langfuse-postgres -- psql -U langfuse -d langfuse -Atc "$1"; }

# 陰性は「ログインできない」だけでなく、issuer まで到達してから拒否されたことも見る
# (fixture に届く前の失敗や設定不足を拒否と取り違えない)。
auth_log() { kubectl logs -n "$NS" deployment/langfuse-web --since=20s | grep -iE 'oauth|callback|signin|sign up|error' | tail -n 4 | cut -c1-400 || true; }

expect_login_refused() {
  local mode="$1" result
  oidc_client mode "$mode" >/dev/null
  result="$(oidc_client direct)"
  echo "$result" | jq -e '.email == null and (.final | contains("error="))' >/dev/null \
    || { echo "FAIL: $mode でログインが拒否されていない: $result" >&2; exit 1; }
  echo "ok refused: $mode"
  auth_log
}

oidc() {
  local result
  # 偽装 header と password では入れない。API の機械認証は ingest step が見ている。
  result="$(oidc_client spoof)"
  echo "$result" | jq -e '.email == null' >/dev/null || { echo "FAIL: 偽装 header でログインできた: $result" >&2; exit 1; }
  result="$(oidc_client password)"
  echo "$result" | jq -e '.email == null' >/dev/null || { echo "FAIL: password でログインできた: $result" >&2; exit 1; }

  for mode in wrong_aud wrong_iss expired bad_sig alg_none unknown_email; do
    expect_login_refused "$mode"
  done
  test "$(sql "select count(*) from users where email = 'intruder@example.invalid'")" = 0 \
    || { echo 'FAIL: 未知の email の user が作られた' >&2; exit 1; }

  oidc_client mode ok >/dev/null
  result="$(oidc_client direct)"
  echo "$result" | jq -e '.email == "owner@example.invalid"' >/dev/null \
    || { auth_log >&2; echo "FAIL: Owner がログインできない: $result" >&2; exit 1; }
  test "$(sql "select m.role from organization_memberships m join users u on u.id = m.user_id where u.email = 'owner@example.invalid' and m.org_id = 'boxp'")" = OWNER \
    || { echo 'FAIL: Owner の組織権限がない' >&2; exit 1; }
  test "$(sql "select count(*) from \"Account\" a join users u on u.id = a.user_id where u.email = 'owner@example.invalid' and a.provider = 'custom'")" = 1 \
    || { echo 'FAIL: Owner に provider が紐付いていない' >&2; exit 1; }

  # gateway: クリックなしで同じ検証付きのログインへ入り、targetPath は自サイトだけに戻す。
  result="$(oidc_client gateway /project/ai-ops)"
  echo "$result" | jq -e '.status == 302 and .starts_at_issuer and .email == "owner@example.invalid" and (.final | endswith("/project/ai-ops"))' >/dev/null \
    || { echo "FAIL: gateway からログインできない: $result" >&2; exit 1; }
  for target in //evil.example/x https://evil.example/x '/\evil.example/x'; do
    result="$(oidc_client gateway "$target")"
    echo "$result" | jq -e '.email == "owner@example.invalid" and (.final | contains("evil.example") | not)' >/dev/null \
      || { echo "FAIL: gateway が外部へ戻した: $target $result" >&2; exit 1; }
  done
  oidc_client mode bad_sig >/dev/null
  result="$(oidc_client gateway)"
  oidc_client mode ok >/dev/null
  echo "$result" | jq -e '.email == null and (.final | contains("error="))' >/dev/null \
    || { echo "FAIL: gateway 経由で不正な token が通った: $result" >&2; exit 1; }
  # 失敗した login は本物の頁を返し、redirect loop にしない。
  result="$(oidc_client gateway-status GET '/auth/sign-in?error=OAuthCallback')"
  echo "$result" | jq -e '.status == 200 and .html' >/dev/null \
    || { echo "FAIL: error 付きの sign-in が本物の頁を返さない: $result" >&2; exit 1; }
  result="$(oidc_client gateway-status POST /auth/sign-in)"
  echo "$result" | jq -e '.status == 404' >/dev/null || { echo "FAIL: gateway が POST を受けた: $result" >&2; exit 1; }
  result="$(oidc_client gateway-status GET /api/public/health)"
  echo "$result" | jq -e '.status == 404' >/dev/null || { echo "FAIL: gateway が他の path を中継した: $result" >&2; exit 1; }
}

probe() {
  local name="$1" namespace="$2" app="$3"
  cat <<YAML | kubectl apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata:
  name: $name
  namespace: $namespace
  labels:
    app: $app
    chainsaw-probe: "true"
spec:
  terminationGracePeriodSeconds: 1
  automountServiceAccountToken: false
  containers:
    - name: probe
      image: python:3.13-alpine
      command: ["python", "-c", "import time; time.sleep(3600)"]
YAML
}

# 接続先は ClusterIP を直接使い、DNS の成否を混ぜない。拒否は「timeout」だけを認める
# (接続拒否や名前解決失敗で偽 green にしない)。
service_ip() { kubectl get service "$2" -n "$1" -o jsonpath='{.spec.clusterIP}'; }
PY_CONNECT='import socket,sys
try:
    socket.create_connection((sys.argv[1], int(sys.argv[2])), 4).close()
except TimeoutError:
    sys.exit(3)'
NODE_CONNECT='const s=require("net").connect({host:process.argv[1],port:+process.argv[2],timeout:4000});s.on("connect",()=>{s.destroy();process.exit(0)});s.on("timeout",()=>process.exit(3));s.on("error",()=>process.exit(1));'

expect() {
  local want="$1" label="$2" code=0
  shift 2
  "$@" >/dev/null 2>&1 || code=$?
  if { [ "$want" = allow ] && [ "$code" -ne 0 ]; } || { [ "$want" = deny ] && [ "$code" -ne 3 ]; }; then
    echo "NetworkPolicy 不一致: $label は $want のはずが exit $code" >&2
    exit 1
  fi
  echo "ok $want: $label"
}

network_policy() {
  kubectl create namespace hermes-agent --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl create namespace langfuse-outside --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  probe hermes-allowed hermes-agent hermes-agent
  probe hermes-other hermes-agent other
  probe spoof langfuse-outside hermes-agent
  probe tunnel "$NS" langfuse-cloudflared
  cat <<'YAML' | kubectl apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata:
  name: outside
  namespace: langfuse-outside
  labels:
    app: outside
spec:
  terminationGracePeriodSeconds: 1
  automountServiceAccountToken: false
  containers:
    - name: server
      image: python:3.13-alpine
      command: ["python", "-m", "http.server", "443"]
---
apiVersion: v1
kind: Service
metadata:
  name: outside
  namespace: langfuse-outside
spec:
  selector:
    app: outside
  ports:
    - port: 443
YAML
  kubectl wait --for=condition=Ready -n hermes-agent pod/hermes-allowed pod/hermes-other --timeout=180s
  kubectl wait --for=condition=Ready -n langfuse-outside pod/spoof pod/outside --timeout=180s
  kubectl wait --for=condition=Ready -n "$NS" pod/tunnel --timeout=180s

  local web minio clickhouse postgres valkey outside
  web="$(service_ip "$NS" langfuse-web)"; minio="$(service_ip "$NS" langfuse-minio)"
  clickhouse="$(service_ip "$NS" langfuse-clickhouse)"; postgres="$(service_ip "$NS" langfuse-postgres)"
  valkey="$(service_ip "$NS" langfuse-valkey)"; outside="$(service_ip langfuse-outside outside)"
  from() { local ns="$1" pod="$2"; shift 2; kubectl exec -n "$ns" "$pod" -- python -c "$PY_CONNECT" "$@"; }
  node_from() { kubectl exec -n "$NS" "deployment/$1" -- node -e "$NODE_CONNECT" "$2" "$3"; }

  # ingress: 名前付き client の :3000 だけ
  expect allow "hermes-agent/app=hermes-agent -> web:3000" from hermes-agent hermes-allowed "$web" 3000
  expect deny "hermes-agent/app=other -> web:3000" from hermes-agent hermes-other "$web" 3000
  expect deny "他 namespace の同 label -> web:3000" from langfuse-outside spoof "$web" 3000
  expect deny "hermes-agent -> minio:9000" from hermes-agent hermes-allowed "$minio" 9000
  expect deny "hermes-agent -> clickhouse:8123" from hermes-agent hermes-allowed "$clickhouse" 8123
  expect deny "hermes-agent -> postgres:5432" from hermes-agent hermes-allowed "$postgres" 5432
  expect deny "hermes-agent -> valkey:6379" from hermes-agent hermes-allowed "$valkey" 6379
  # egress: 到達可能な外部 (陽性対照) へ、Web/Worker は出られず tunnel だけ出られる
  expect allow "陽性対照: 制限なし namespace -> outside:443" from langfuse-outside spoof "$outside" 443
  expect deny "web -> outside:443" node_from langfuse-web "$outside" 443
  expect deny "worker -> outside:443" node_from langfuse-worker "$outside" 443
  expect allow "web -> clickhouse:8123 (namespace 内)" node_from langfuse-web "$clickhouse" 8123
  expect allow "cloudflared label -> outside:443" from "$NS" tunnel "$outside" 443
  expect allow "cloudflared label -> web:3000" from "$NS" tunnel "$web" 3000
}

otlp() {
  kubectl exec -i -n hermes-agent hermes-allowed -- \
    env "LF_PK=$(secret_value langfuse-ingest-key LANGFUSE_PUBLIC_KEY)" "LF_SK=$(secret_value langfuse-ingest-key LANGFUSE_SECRET_KEY)" \
    python - "$@" < "$SUITE/otlp.py"
}

ingest() {
  otlp auth
  otlp send "$TRACE_INGEST" 0 0 chainsaw-body-marker
  otlp present "$TRACE_INGEST" chainsaw-body-marker 240
  # 本文は ClickHouse まで届いている (web/worker/Valkey/MinIO/ClickHouse の実連携)。
  local objects
  objects="$(kubectl exec -n "$NS" statefulset/langfuse-minio -- ls -A /data/langfuse/events/otel 2>/dev/null || true)"
  test -n "$objects" || { echo 'raw event が MinIO にありません' >&2; exit 1; }
}

clickhouse_count() {
  kubectl exec -n "$NS" statefulset/langfuse-clickhouse -- clickhouse-client --user langfuse \
    --password "$(secret_value langfuse-secrets CLICKHOUSE_PASSWORD)" \
    --query "SELECT count() FROM events_core FINAL WHERE trace_id = '$1'"
}

retention() {
  otlp send "$TRACE_OLD" 15 0 chainsaw-old
  otlp send "$TRACE_SHARED" 0 1 chainsaw-shared
  otlp send "$TRACE_FRESH" 0 0 chainsaw-fresh
  local trace
  for trace in "$TRACE_OLD" "$TRACE_SHARED" "$TRACE_FRESH"; do otlp present "$trace" chainsaw 240; done

  # 実 CronJob の spec のまま 1 回起動する。OPTIMIZE は UTC の特定時刻だけなので明示する。
  kubectl get cronjob langfuse-retention -n "$NS" -o json | jq '{apiVersion: "batch/v1", kind: "Job",
      metadata: {name: "retention-chainsaw", namespace: .metadata.namespace}, spec: .spec.jobTemplate.spec}
    | .spec.template.spec.containers[0].env += [{name: "FORCE_OPTIMIZE", value: "true"}]' | kubectl apply -f - >/dev/null
  kubectl wait -n "$NS" --for=condition=Complete job/retention-chainsaw --timeout=240s \
    || { kubectl logs -n "$NS" job/retention-chainsaw || true; diagnose; exit 1; }
  local report
  report="$(kubectl logs -n "$NS" job/retention-chainsaw | tail -n 1)"
  echo "retention report: $report"
  jq -e '.expired_traces_deleted >= 1 and .public_traces_deleted >= 1 and .optimized == true and .lifecycle == ["events/:1d"]
    and ([keys[] | select(endswith("_error"))] | length == 0)' <<<"$report" >/dev/null \
    || { echo 'retention report が想定と異なります' >&2; exit 1; }

  otlp gone "$TRACE_OLD" 300
  otlp gone "$TRACE_SHARED" 300
  otlp present "$TRACE_FRESH" chainsaw-fresh 30
  test "$(clickhouse_count "$TRACE_OLD")" = 0 || { echo '期限切れ trace の行が ClickHouse に残っています' >&2; exit 1; }
  test "$(clickhouse_count "$TRACE_SHARED")" = 0 || { echo '共有 trace の行が ClickHouse に残っています' >&2; exit 1; }
  test "$(clickhouse_count "$TRACE_FRESH")" -gt 0 || { echo 'fresh trace まで消えています' >&2; exit 1; }
}

TRACE_INGEST=a1000000000000000000000000000001
TRACE_OLD=a2000000000000000000000000000002
TRACE_SHARED=a3000000000000000000000000000003
TRACE_FRESH=a4000000000000000000000000000004
"$1"
