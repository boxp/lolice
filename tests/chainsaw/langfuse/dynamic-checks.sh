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

# 本番 PVC の postgres データ形式。pg_upgrade で本番データを上げたらここも更新する。
PRODUCTION_DATA_POSTGRES=docker.io/postgres:16.15

# 空 PVC からの起動だけでは major 更新の非互換を検出できない (lolice#1060)。
# 本番 StatefulSet のコンテナ定義をそのまま使い、既存形式のデータで起動して読めることを確認する。
postgres_existing_data() {
  local pod=langfuse-postgres-existing-data
  kubectl get statefulset langfuse-postgres -n "$NS" -o json | jq --arg seed "$PRODUCTION_DATA_POSTGRES" --arg pod "$pod" '
    .spec.template.spec as $s | $s.containers[0] as $c
    | {apiVersion: "v1", kind: "Pod", metadata: {name: $pod, namespace: "langfuse", labels: {"chainsaw-probe": "true"}},
       spec: {restartPolicy: "Never", securityContext: $s.securityContext, containers: [$c],
         volumes: [{name: "data", emptyDir: {}}],
         initContainers: [{name: "seed", image: $seed, securityContext: $c.securityContext, volumeMounts: $c.volumeMounts,
           env: [$c.env[] | select(.name == "PGDATA")], command: ["bash", "-ec",
             "initdb -D \"$PGDATA\" -U langfuse -A trust >/dev/null; pg_ctl -D \"$PGDATA\" -w -o \"-c listen_addresses=\" start >/dev/null; createdb -U langfuse langfuse; psql -U langfuse -d langfuse -c \"create table chainsaw_marker as select 42 as v\" >/dev/null; pg_ctl -D \"$PGDATA\" -w -m fast stop >/dev/null"]}]}}' \
    | kubectl apply -f - >/dev/null
  if ! kubectl wait -n "$NS" --for=condition=Ready "pod/$pod" --timeout=120s; then
    kubectl logs -n "$NS" "$pod" -c seed --tail=10 || true
    kubectl logs -n "$NS" "$pod" -c postgres --tail=10 || true
    echo 'FAIL: 本番 postgres image が既存データ形式で起動しない' >&2
    exit 1
  fi
  test "$(kubectl exec -n "$NS" "$pod" -- psql -U langfuse -d langfuse -Atc 'select v from chainsaw_marker')" = 42 \
    || { echo 'FAIL: 既存データを読めない' >&2; exit 1; }
  kubectl delete pod -n "$NS" "$pod" --wait=false >/dev/null
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

# 陰性は「ログインできない」だけでなく、期待した理由で拒否されたことを web のログで見る
# (timeout や設定不足による失敗を拒否と取り違えない)。
auth_log() { kubectl logs -n "$NS" deployment/langfuse-web --since=20s | grep -iE 'oauth|callback|signin|sign up|error' | tail -n 4 | cut -c1-400 || true; }

expect_login_refused() {
  local mode="$1" reason="$2" result
  oidc_client mode "$mode" >/dev/null
  result="$(oidc_client direct)"
  echo "$result" | jq -e '.email == null and (.final | contains("error="))' >/dev/null \
    || { echo "FAIL: $mode でログインが拒否されていない: $result" >&2; exit 1; }
  kubectl logs -n "$NS" deployment/langfuse-web --since=20s | grep -qF "$reason" \
    || { auth_log >&2; echo "FAIL: $mode が期待した理由で拒否されていない: $reason" >&2; exit 1; }
  echo "ok refused: $mode ($reason)"
}

oidc() {
  local result
  # 偽装 header と password では入れない。API の機械認証は ingest step が見ている。
  result="$(oidc_client spoof)"
  echo "$result" | jq -e '.email == null' >/dev/null || { echo "FAIL: 偽装 header でログインできた: $result" >&2; exit 1; }
  result="$(oidc_client password)"
  echo "$result" | jq -e '.email == null' >/dev/null || { echo "FAIL: password でログインできた: $result" >&2; exit 1; }

  expect_login_refused wrong_aud 'aud mismatch'
  expect_login_refused wrong_iss 'unexpected iss value'
  expect_login_refused expired 'JWT expired'
  expect_login_refused bad_sig 'failed to validate JWT signature'
  expect_login_refused alg_none 'unexpected JWT alg received'
  expect_login_refused unknown_email 'Sign up is disabled'
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
  # 署名済みでない頁から入ると、Langfuse は /auth/sign-in へ client 側遷移し router が props を
  # /_next/data/<buildId>/auth/sign-in.json に取りに来る。ここでもボタンなしで issuer へ進むこと。
  local build
  build="$(oidc_client build-id | jq -r .build)"
  test -n "$build" && test "$build" != null || { echo 'FAIL: web の buildId が読めない' >&2; exit 1; }
  result="$(oidc_client gateway-data "$build" /project/ai-ops)"
  echo "$result" | jq -e '.status == 200 and .starts_at_issuer and .email == "owner@example.invalid" and (.final | endswith("/project/ai-ops"))' >/dev/null \
    || { echo "FAIL: client 側遷移の sign-in でボタンなしのログインが始まらない: $result" >&2; exit 1; }
  result="$(oidc_client gateway-data "$build" //evil.example/x)"
  echo "$result" | jq -e '.email == "owner@example.invalid" and (.final | contains("evil.example") | not)' >/dev/null \
    || { echo "FAIL: client 側遷移の gateway が外部へ戻した: $result" >&2; exit 1; }
  result="$(oidc_client gateway-status GET "/_next/data/$build/auth/sign-in.json?error=OAuthCallback")"
  echo "$result" | jq -e '.status == 200 and (.location == "")' >/dev/null \
    || { echo "FAIL: error 付きの props 要求が本物の props を返さない: $result" >&2; exit 1; }
  result="$(oidc_client gateway-status GET "/_next/data/$build/project.json")"
  echo "$result" | jq -e '.status == 404' >/dev/null || { echo "FAIL: gateway が他の props を中継した: $result" >&2; exit 1; }
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

# hermes-agent の本番 SecretStore/ExternalSecret で、実際に ESO Kubernetes provider から ingest key を取得する。
# codex-workspace も自分の ServiceAccount と本番 resource で同じ確認をする。
producer_ingest_key() {
  local manifest key producer
  manifest="$(mktemp)"
  for producer in hermes-agent codex-workspace; do
    kubectl create namespace "$producer" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
    kustomize build "$REPO_ROOT/argoproj/$producer/" \
      | yq 'select((.kind == "SecretStore" or .kind == "ExternalSecret" or .kind == "ServiceAccount") and .metadata.name == "langfuse-ingest-key*")' > "$manifest"
    test "$(yq -N '.kind' "$manifest" | sort | tr '\n' ' ')" = "ExternalSecret SecretStore ServiceAccount "
    kubectl apply --server-side -f "$manifest" >/dev/null
    kubectl wait -n "$producer" --for=condition=Ready secretstore/langfuse-ingest-key externalsecret/langfuse-ingest-key --timeout=120s \
      || { kubectl describe -n "$producer" secretstore,externalsecret || true; echo "FAIL: $producer の SecretStore が ingest key を取得できない" >&2; exit 1; }
    for key in LANGFUSE_PUBLIC_KEY LANGFUSE_SECRET_KEY; do
      test "$(kubectl get secret langfuse-ingest-key -n "$producer" -o json | jq -er --arg key "$key" '.data[$key] | @base64d')" = "$(secret_value langfuse-ingest-key "$key")" \
        || { echo "FAIL: $producer の $key が langfuse 側と一致しない" >&2; exit 1; }
    done
  done
  rm -f "$manifest"
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

mcp_readonly_proxy() {
  local proxy web logs key
  proxy="$(service_ip "$NS" langfuse-mcp-readonly)"
  web="$(service_ip "$NS" langfuse-web)"
  expect deny "outside -> MCP proxy:3002" kubectl exec -n langfuse-outside spoof -- python -c "$PY_CONNECT" "$proxy" 3002
  kubectl exec -n "$NS" deployment/langfuse-mcp-readonly -- node -e "$NODE_CONNECT" "$(service_ip langfuse-outside outside)" 443 >/dev/null 2>&1 && { echo 'FAIL: proxy has outside egress' >&2; return 1; }
  if ! kubectl exec -i -n hermes-agent hermes-allowed -- env "LF_PK=$(secret_value langfuse-ingest-key LANGFUSE_PUBLIC_KEY)" "LF_SK=$(secret_value langfuse-ingest-key LANGFUSE_SECRET_KEY)" python - "$proxy" "$web" "$TRACE_INGEST" <<'PYTEST'
import datetime, json, os, sys, urllib.request, urllib.error
proxy, web, trace = sys.argv[1:]
def request(path, body=None, headers=None, host=proxy, port=3002):
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    req = urllib.request.Request(f'http://{host}:{port}{path}', data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read()
            assert all(os.environ[k].encode() not in body for k in ('LF_PK','LF_SK')), 'key leaked in response'
            return r.status, body
    except urllib.error.HTTPError as e: return e.code, e.read()
def rpc(method, params=None):
    status, body = request('/mcp', {'jsonrpc':'2.0','id':1,'method':method,'params':params or {}}, {'Authorization':'Basic bogus','Content-Type':'application/json'})
    assert status == 200
    return json.loads(body)
assert request('/healthz') == (200,b'ok')
r = rpc('initialize', {'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'chainsaw','version':'1'}})
assert 'result' in r, f"initialize failed: {r.get('error')}"
assert r['result']['serverInfo'] and not ({'resources','prompts'} & r['result']['capabilities'].keys())
r = rpc('tools/list'); names = {t['name'] for t in r['result']['tools']}
assert names == {'listObservations','getObservation','getObservationFieldSchema','getObservationFilterSchema','getObservationFilterValues','getHealth'}
assert not names & {'createTextPrompt','deleteDatasetItem','submitFeedback','createScore'}
assert rpc('tools/call', {'name':'createScore','arguments':{}})['error']['code'] == -32602
for method in ('prompts/list','resources/list'): assert rpc(method)['error']['code'] == -32601
now = datetime.datetime.now(datetime.timezone.utc)
start = (now - datetime.timedelta(minutes=59)).isoformat(); end = (now + datetime.timedelta(minutes=1)).isoformat()
args = {'traceId':trace,'fromStartTime':start,'toStartTime':end,'fields':['id','traceId']}
r = rpc('tools/call', {'name':'listObservations','arguments':args})
def objects(x):
    if isinstance(x,dict):
        yield x
        for v in x.values(): yield from objects(v)
    elif isinstance(x,list):
        for v in x: yield from objects(v)
    elif isinstance(x,str):
        try: yield from objects(json.loads(x))
        except (ValueError,RecursionError): pass
matches = [v for v in objects(r) if v.get('traceId') == trace and v.get('id')]
assert matches, 'ingested observation missing'
args.update(observationId=matches[0]['id'], fields=['id','traceId','input','output'])
r = rpc('tools/call', {'name':'getObservation','arguments':args})
assert 'chainsaw-body-marker' in json.dumps(r), 'observation body missing'
status, _ = request('/api/public/mcp', {'jsonrpc':'2.0','id':1,'method':'tools/list'}, {'Host':'langfuse-web.langfuse.svc.cluster.local:3000','Content-Type':'application/json'}, web, 3000)
assert status in (401,403)
assert request('/mcp', b' '*65537)[0] == 413
assert json.loads(request('/mcp', b'[]')[1])['error']['code'] == -32600
print('PASS: real Langfuse MCP proxy checks')
PYTEST
  then
    # 本文を出さない proxy の 1 行ログ (method/tool/decision/upstream status) だけを診断に使う。
    kubectl logs -n "$NS" deployment/langfuse-mcp-readonly --tail=20 >&2 || true
    return 1
  fi
  logs="$(kubectl logs -n "$NS" deployment/langfuse-mcp-readonly)"
  [[ "$logs" == *'decision=denied'* ]] || { echo 'FAIL: denied request not logged' >&2; return 1; }
  for key in LANGFUSE_PUBLIC_KEY LANGFUSE_SECRET_KEY; do
    [[ "$logs" != *"$(secret_value langfuse-ingest-key "$key")"* ]] || { echo 'FAIL: key in logs' >&2; return 1; }
  done
}

# BOXP-472: Grafana 用 user は本文なしの view だけを読め、集計は GENERATION を一度ずつ数える。
usage_view() {
  kubectl wait -n "$NS" --for=condition=Complete job/langfuse-usage-view --timeout=600s \
    || { kubectl logs -n "$NS" job/langfuse-usage-view || true; diagnose; exit 1; }
  secret_value langfuse-grafana-clickhouse CLICKHOUSE_GRAFANA_PASSWORD | grep -Eq '^[A-Za-z0-9]{48}$'
  test "$(secret_value langfuse-grafana-clickhouse CLICKHOUSE_GRAFANA_PASSWORD)" != "$(secret_value langfuse-secrets CLICKHOUSE_PASSWORD)"

  local answer verb_resource
  answer="$(kubectl auth can-i get secret/langfuse-grafana-clickhouse -n "$NS" --as=system:serviceaccount:monitoring:langfuse-grafana-clickhouse-reader || true)"
  test "$answer" = yes
  for verb_resource in "get secret/langfuse-secrets" "get secret/langfuse-ingest-key" "list secrets" "watch secrets" "update secret/langfuse-grafana-clickhouse"; do
    # shellcheck disable=SC2086
    answer="$(kubectl auth can-i $verb_resource -n "$NS" --as=system:serviceaccount:monitoring:langfuse-grafana-clickhouse-reader || true)"
    test "$answer" = no || { echo "monitoring の reader に過剰な権限: $verb_resource" >&2; exit 1; }
  done

  local admin grafana
  admin="$(secret_value langfuse-secrets CLICKHOUSE_PASSWORD)"
  grafana="$(secret_value langfuse-grafana-clickhouse CLICKHOUSE_GRAFANA_PASSWORD)"
  as_admin() { kubectl exec -i -n "$NS" statefulset/langfuse-clickhouse -- clickhouse-client --user langfuse --password "$admin" "$@"; }
  as_grafana() { kubectl exec -n "$NS" statefulset/langfuse-clickhouse -- clickhouse-client --user grafana_usage --password "$grafana" "$@"; }

  # 親 SPAN/AGENT に usage を持たせ、二重加算されないことも確かめる。同じ span の再送は FINAL で 1 行になる。
  # other-project の行は集計にも親 AGENT 名の解決にも混ざらないことを確かめる。
  as_admin --multiquery <<'SQL'
INSERT INTO events_core (project_id, trace_id, span_id, parent_span_id, start_time, name, type, trace_name, tags, provided_model_name, usage_details, input, output, event_ts, is_deleted) VALUES
('ai-ops', 'viewtb', 'root', '', toDateTime64('2026-11-09 00:00:00', 6), 'task-board-run BOXP-1', 'SPAN', 'task-board-run BOXP-1', ['task-board'], '', map('total', 999999), '', '', now64(6), 0),
('ai-ops', 'viewtb', 'child', 'root', toDateTime64('2026-11-09 00:00:01', 6), 'delegate:codex', 'AGENT', 'task-board-run BOXP-1', ['task-board'], '', map('total', 888888), '', '', now64(6), 0),
('ai-ops', 'viewtb', 'gen1', 'child', toDateTime64('2026-11-09 00:00:02', 6), 'generation:gpt-6.1-terra', 'GENERATION', 'task-board-run BOXP-1', ['task-board'], 'gpt-6.1-terra', map('input', 10, 'input_cached_tokens', 90, 'output', 5, 'output_reasoning_tokens', 1, 'total', 106), 'usage-view-body-marker', 'usage-view-body-marker', now64(6), 0),
('ai-ops', 'viewtb', 'gen2', 'root', toDateTime64('2026-11-09 00:00:03', 6), 'generation:claude-opus-5-5', 'GENERATION', 'task-board-run BOXP-1', ['task-board'], 'claude-opus-5-5', map('input', 1, 'cache_read_input_tokens', 100, 'cache_creation_input_tokens', 20, 'output', 3, 'total', 124), 'usage-view-body-marker', '', now64(6), 0),
('ai-ops', 'viewtb', 'gen2', 'root', toDateTime64('2026-11-09 00:00:03', 6), 'generation:claude-opus-5-5', 'GENERATION', 'task-board-run BOXP-1', ['task-board'], 'claude-opus-5-5', map('input', 1, 'cache_read_input_tokens', 100, 'cache_creation_input_tokens', 20, 'output', 3, 'total', 124), 'usage-view-body-marker', '', now64(6), 0),
('ai-ops', 'viewtb', 'gen3', 'root', toDateTime64('2026-11-09 00:00:04', 6), 'generation:x', 'GENERATION', 'hermes-turn', ['hermes', 'platform:cron'], 'gpt-6.1-sol', map('input', 7, 'total', 7), '', '', now64(6), 0),
('ai-ops', 'viewtb', 'gen4', 'root', toDateTime64('2026-11-09 00:00:05', 6), 'generation:x', 'GENERATION', 'something-else', [], '', map(), '', '', now64(6), 0),
('other-project', 'viewtb', 'gen-other', 'child', toDateTime64('2026-11-09 00:00:06', 6), 'generation:x', 'GENERATION', 'task-board-run BOXP-1', ['task-board'], 'gpt-6.1-terra', map('input', 5000, 'total', 5000), '', '', now64(6), 0),
('other-project', 'viewtb', 'gen1', 'other-agent', toDateTime64('2026-11-09 00:00:07', 6), 'generation:x', 'GENERATION', 'hermes-turn', ['hermes'], 'claude-opus-5-5', map('input', 6000, 'total', 6000), '', '', now64(6), 0),
('other-project', 'viewtb', 'child', 'root', toDateTime64('2026-11-09 00:00:08', 6), 'subagent:other', 'AGENT', 'task-board-run BOXP-1', ['task-board'], '', map(), '', '', now64(6), 0);
SQL
  local got expected
  expected="$(printf 'Hermes / cron\tcodex\t7\t1\nTask Board / 子委譲 (Codex)\tcodex\t106\t1\nTask Board / 親 (CLI)\tclaude\t124\t1\n(未分類)\t(不明)\t0\t0' | LC_ALL=C sort)"
  got="$(as_grafana --query "SELECT route, provider, sum(total_tokens), countIf(has_usage) FROM ai_ops_usage_v1 WHERE trace_id = 'viewtb' GROUP BY route, provider FORMAT TSV" | LC_ALL=C sort)"
  test "$got" = "$expected" || { printf 'usage view の集計が想定と異なります:\n%s\n' "$got" >&2; exit 1; }
  got="$(as_grafana --query "SELECT sum(cache_read_tokens), sum(cache_write_tokens), sum(input_tokens + output_tokens + reasoning_tokens), sum(other_tokens) FROM ai_ops_usage_v1 WHERE trace_id = 'viewtb' FORMAT TSV")"
  test "$got" = "$(printf '190\t20\t27\t0')" || { echo "token 区分の正規化が想定と異なります: $got" >&2; exit 1; }
  ! as_grafana --query "SELECT * FROM ai_ops_usage_v1 WHERE trace_id = 'viewtb' FORMAT TSV" | grep -q usage-view-body-marker \
    || { echo 'view から本文が読めます' >&2; exit 1; }

  denied() { if as_grafana --query "$1" >/dev/null 2>&1; then echo "grafana_usage に許可されています: $1" >&2; exit 1; fi; echo "ok denied: $1"; }
  denied "SELECT input FROM ai_ops_usage_v1 LIMIT 1"
  denied "SELECT count() FROM events_core"
  denied "SELECT count() FROM events_full"
  denied "INSERT INTO events_core (project_id) VALUES ('x')"
  denied "DROP VIEW ai_ops_usage_v1"
  denied "SELECT 1 SETTINGS max_execution_time = 3600"

  # grafana の label だけが :8123 に届き、他の DB には届かない。
  kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  usage_probe() {
    cat <<YAML | kubectl apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata:
  name: $1
  namespace: $2
  labels:
    app.kubernetes.io/name: $3
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
  usage_probe grafana-allowed monitoring grafana
  usage_probe grafana-other monitoring prometheus
  usage_probe grafana-spoof langfuse-outside grafana
  kubectl wait --for=condition=Ready -n monitoring pod/grafana-allowed pod/grafana-other --timeout=180s
  kubectl wait --for=condition=Ready -n langfuse-outside pod/grafana-spoof --timeout=180s
  local clickhouse postgres web
  clickhouse="$(service_ip "$NS" langfuse-clickhouse)"; postgres="$(service_ip "$NS" langfuse-postgres)"; web="$(service_ip "$NS" langfuse-web)"
  from() { local ns="$1" pod="$2"; shift 2; kubectl exec -n "$ns" "$pod" -- python -c "$PY_CONNECT" "$@"; }
  expect allow "monitoring/grafana -> clickhouse:8123" from monitoring grafana-allowed "$clickhouse" 8123
  expect deny "monitoring/grafana -> clickhouse:9000" from monitoring grafana-allowed "$clickhouse" 9000
  expect deny "monitoring/grafana -> postgres:5432" from monitoring grafana-allowed "$postgres" 5432
  expect deny "monitoring/grafana -> web:3000" from monitoring grafana-allowed "$web" 3000
  expect deny "monitoring/other -> clickhouse:8123" from monitoring grafana-other "$clickhouse" 8123
  expect deny "他 namespace の同 label -> clickhouse:8123" from langfuse-outside grafana-spoof "$clickhouse" 8123

  as_admin --query "ALTER TABLE events_core DELETE WHERE trace_id = 'viewtb' SETTINGS mutations_sync = 1"
}

clickhouse_count() {
  kubectl exec -n "$NS" statefulset/langfuse-clickhouse -- clickhouse-client --user langfuse \
    --password "$(secret_value langfuse-secrets CLICKHOUSE_PASSWORD)" \
    --query "SELECT count() FROM events_core FINAL WHERE trace_id = '$1'"
}

seed_s3_objects() {
  # 実 CronJob と同じ env/NetPol で、一覧 20 ページ (2 万件) を超える object を置く。
  kubectl get cronjob langfuse-retention -n "$NS" -o json | jq --rawfile script "$SUITE/seed-s3-objects.js" --arg count "$S3_SEED_COUNT" '{apiVersion: "batch/v1", kind: "Job",
      metadata: {name: "retention-seed", namespace: .metadata.namespace}, spec: .spec.jobTemplate.spec}
    | .spec.template.spec.containers[0].command = ["node", "-e", $script]
    | .spec.template.spec.containers[0].resources.limits = {cpu: "2", memory: "512Mi"}
    | .spec.template.spec.containers[0].env += [{name: "SEED_COUNT", value: $count}]' | kubectl apply -f - >/dev/null
  kubectl wait -n "$NS" --for=condition=Complete job/retention-seed --timeout=300s \
    || { kubectl logs -n "$NS" job/retention-seed || true; diagnose; exit 1; }
}

retention() {
  # ingest step の raw event は seed より古く、対象マーカーの raw event は seed より新しい。
  local seed_started
  seed_started="$(date +%s)"
  seed_s3_objects
  otlp send "$TRACE_OLD" 15 0 BOXP428SYNTH
  otlp send "$TRACE_SHARED" 0 1 chainsaw-shared
  otlp send "$TRACE_FRESH" 0 0 BOXP428CTRL
  local trace
  otlp present "$TRACE_OLD" BOXP428SYNTH 240
  otlp present "$TRACE_SHARED" chainsaw 240
  otlp present "$TRACE_FRESH" BOXP428CTRL 240

  # 実 CronJob の spec のまま 1 回起動する。OPTIMIZE は UTC の特定時刻だけなので明示する。
  kubectl get cronjob langfuse-retention -n "$NS" -o json | jq '{apiVersion: "batch/v1", kind: "Job",
      metadata: {name: "retention-chainsaw", namespace: .metadata.namespace}, spec: .spec.jobTemplate.spec}
    | .spec.template.spec.containers[0].env += [{name: "FORCE_OPTIMIZE", value: "true"}]' | kubectl apply -f - >/dev/null
  kubectl wait -n "$NS" --for=condition=Complete job/retention-chainsaw --timeout=240s \
    || { kubectl logs -n "$NS" job/retention-chainsaw || true; diagnose; exit 1; }
  local report
  report="$(kubectl logs -n "$NS" job/retention-chainsaw | tail -n 1)"
  jq -e '.expired_traces_deleted >= 1 and .public_traces_deleted >= 1 and .optimized == true and .lifecycle == ["events/:1d"]
    and .audit.traces > 0 and .audit.control_rows_ch >= 1 and .audit.marker_rows_ch >= 1 and .audit.marker_objects_s3.matches >= 1 and .audit.marker_objects_s3.scanned >= 1
    and ([keys[] | select(endswith("_error"))] | length == 0)' <<<"$report" >/dev/null \
    || { echo "retention report が想定と異なります: $report" >&2; exit 1; }
  # キー順で後ろにある実 raw event まで一覧できている (最古は seed より前の ingest、新しい順の先頭はマーカー)。
  jq -e --argjson count "$S3_SEED_COUNT" --argjson min_age "$(( $(date +%s) - seed_started ))" '.audit.s3.truncated == false and .audit.s3.objects > $count
    and .audit.s3.oldest_age_hours * 3600 >= $min_age
    and .audit.marker_objects_s3.unscanned == (.audit.s3.objects - .audit.marker_objects_s3.scanned)' <<<"$report" >/dev/null \
    || { echo "retention report の S3 走査が 2 万件を超える object を扱えていません: $report" >&2; exit 1; }
  for trace in "$TRACE_OLD" "$TRACE_SHARED" "$TRACE_FRESH" "$TRACE_INGEST" BOXP428SYNTH BOXP428CTRL chainsaw-old chainsaw-shared chainsaw-body-marker; do
    [[ "$report" != *"$trace"* ]] || { echo 'retention report に本文または trace ID が含まれています' >&2; exit 1; }
  done

  otlp gone "$TRACE_OLD" 300
  otlp gone "$TRACE_SHARED" 300
  otlp present "$TRACE_FRESH" BOXP428CTRL 30
  test "$(clickhouse_count "$TRACE_OLD")" = 0 || { echo '期限切れ trace の行が ClickHouse に残っています' >&2; exit 1; }
  test "$(clickhouse_count "$TRACE_SHARED")" = 0 || { echo '共有 trace の行が ClickHouse に残っています' >&2; exit 1; }
  test "$(clickhouse_count "$TRACE_FRESH")" -gt 0 || { echo 'fresh trace まで消えています' >&2; exit 1; }
  kubectl get cronjob langfuse-retention -n "$NS" -o json | jq '{apiVersion: "batch/v1", kind: "Job",
      metadata: {name: "retention-chainsaw-after", namespace: .metadata.namespace}, spec: .spec.jobTemplate.spec}
    | .spec.template.spec.containers[0].env += [{name: "FORCE_OPTIMIZE", value: "true"}]' | kubectl apply -f - >/dev/null
  kubectl wait -n "$NS" --for=condition=Complete job/retention-chainsaw-after --timeout=240s \
    || { echo '削除後の retention Job が失敗しました' >&2; exit 1; }
  report="$(kubectl logs -n "$NS" job/retention-chainsaw-after | tail -n 1)"
  jq -e '.audit.marker_rows_ch == 0 and .audit.control_rows_ch >= 1 and .optimized == true and ([keys[] | select(endswith("_error"))] | length == 0)' <<<"$report" >/dev/null
  for trace in "$TRACE_OLD" "$TRACE_SHARED" "$TRACE_FRESH" "$TRACE_INGEST" BOXP428SYNTH BOXP428CTRL chainsaw-shared chainsaw-body-marker; do
    [[ "$report" != *"$trace"* ]] || { echo '削除後の report に本文または trace ID が含まれています' >&2; exit 1; }
  done
}

S3_SEED_COUNT=20001
TRACE_INGEST=a1000000000000000000000000000001
TRACE_OLD=a2000000000000000000000000000002
TRACE_SHARED=a3000000000000000000000000000003
TRACE_FRESH=a4000000000000000000000000000004
"$1"
