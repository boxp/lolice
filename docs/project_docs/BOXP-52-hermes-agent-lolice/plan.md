# BOXP-52: lolice cluster で Hermes Agent を常時稼働する計画

## Goal

lolice cluster 上に NousResearch Hermes Agent を常時稼働させ、外部 hosted LLM API ではなく既存の local LLM `gemma4-26b-vision` を backend として使う。

このチケットでは実行計画を固めたうえで、lolice GitOps manifest の最小実装まで追加する。Hermes の公式 Docker image を使い、実行環境、設定、PVC、Obsidian headless sync、NetworkPolicy、疎通確認、運用・障害切り分けをレビュー可能な形で残す。

## Confirmed Facts

- Hermes Agent の入手元は `NousResearch/hermes-agent`。
- Hermes Agent は Linux/macOS/WSL2 向けに `curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash` で CLI を導入できる。
- Hermes Agent は `hermes model` / `config.yaml` で custom endpoint を設定でき、OpenAI-compatible な `/v1/chat/completions` endpoint に接続できる。
- Hermes Agent は self-hosted endpoint / vLLM / Ollama などを custom provider として扱える。
- Hermes Agent の設定と永続状態は通常 `~/.hermes/` 配下に置かれる。
- Gateway は公式 Docker image では `nousresearch/hermes-agent gateway run` で起動し、`/opt/data` に mutable state を置く。
- 公式 Docker image は s6-overlay を entrypoint として使い、起動時に `/opt/data` の ownership 修正と config migration を行ったあと、gateway process を `hermes` user で supervised 実行する。
- API server を使う場合は `API_SERVER_ENABLED=true`、`API_SERVER_HOST=0.0.0.0`、`API_SERVER_KEY` を設定し、port 8642 の `/health` と OpenAI-compatible API を使う。ただし API server は local terminal backend を露出するため、今回の常時稼働 manifest では無効化する。
- lolice の local LLM は `argoproj/local-llm` で GitOps 管理されている。
- lolice cluster 内の OpenAI-compatible endpoint は `http://llama-server.local-llm.svc.cluster.local:8080/v1`。
- `argoproj/local-llm/models-config.yaml` では `gemma4-26b-vision` がモデル ID として定義済み。
- `gemma4-26b-vision` は `Gemma4-26B-A4B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M.gguf` と `mmproj-Gemma4-26B-A4B-QAT-Uncensored-HauhauCS-Balanced-BF16.gguf` を使う vision model として定義済み。
- `argoproj/codex-workspace/configmap.yaml` の pi agent 設定では `gemma4-26b-vision` が `input: ["text", "image"]`、`contextWindow: 262144`、`maxTokens: 4096` として公開済み。
- `argoproj/codex-workspace/deployment.yaml` には `ob sync --path "${OBSIDIAN_VAULT_PATH}" --continuous` を sidecar として動かし、`/home/boxp/Documents/obsidian-headless/BOXP` を維持する実装例がある。
- `argoproj/codex-workspace/networkpolicy.yaml` には `codex-workspace` から `local-llm` namespace の `app == 'llama-server'` TCP 8080 への egress 許可例と、Obsidian sync 用の外向き通信許可例がある。

## External References

- Hermes Agent repository: https://github.com/NousResearch/hermes-agent
- Hermes Agent install docs: https://hermes-agent.nousresearch.com/docs/getting-started/installation
- Hermes Agent provider docs: https://hermes-agent.nousresearch.com/docs/integrations/providers
- Hermes Agent messaging gateway docs: https://hermes-agent.nousresearch.com/docs/user-guide/messaging/
- Hermes Agent Docker docs: https://hermes-agent.nousresearch.com/docs/user-guide/docker

## Target Architecture

第一候補は Kubernetes Deployment + Argo CD/GitOps 管理とする。systemd、tmux、手動 Pod は bootstrap や一時検証だけに限定する。

```text
user / messaging platform / Web UI
  -> Cloudflare Access GitHub authentication for hermes-agent.b0xp.io
  -> Cloudflare Tunnel
  -> cloudflared sidecar in Hermes Agent Pod
  -> Hermes dashboard on 127.0.0.1:9119 in same Pod
  -> Hermes Agent gateway Pod
  -> Hermes Agent runtime in same Pod
  -> custom OpenAI-compatible provider
  -> http://llama-server.local-llm.svc.cluster.local:8080/v1
  -> local-llm Deployment on GPU worker
  -> gemma4-26b-vision

Hermes Agent Pod
  -> obsidian-sync sidecar
  -> /home/boxp/Documents/obsidian-headless/BOXP on shared Longhorn PVC
```

Kubernetes resources:

| Resource | 方針 |
| --- | --- |
| Repository | `boxp/lolice` |
| Directory | `argoproj/hermes-agent/` |
| Argo CD Application | `argoproj/hermes-agent/argocd-application.yaml` |
| Namespace | `hermes-agent` |
| Workload | `Deployment/hermes-agent`, replicas 1 |
| Scheduling | Hermes image と ImageUpdater の platform を安定させるため amd64 node に固定 |
| Service | なし。Web UI は同一 Pod の cloudflared sidecar から `127.0.0.1:9119` へ転送する |
| PVC | Longhorn PVC 1 個を `/opt/data` と `/home/boxp` に mount |
| ConfigMap | `config.yaml`、non-secret の model/provider/runtime 設定 |
| Secret / ExternalSecret | Cloudflare Tunnel token は ExternalSecret。messaging token や API key が必要になった時点で追加 |
| Obsidian headless | `node:22-bookworm-slim` の initContainer で `obsidian-headless@0.0.12` を install し、同じ Node image の sidecar で `ob sync --continuous` を実行 |
| Image updates | `argoproj/argocd-image-updater/imageupdaters/hermes-agent.yaml` で Hermes image、Obsidian sidecar image、cloudflared image を追跡 |
| NetworkPolicy | default deny 前提。DNS、local-llm:8080、Obsidian sync / messaging に必要な outbound のみ許可 |
| Observability | stdout/stderr logs。必要なら ServiceMonitor は後続で追加 |

Web UI public access:

| Item | Decision |
| --- | --- |
| Public hostname | `https://hermes-agent.b0xp.io` |
| Auth boundary | Cloudflare Access GitHub IdP 必須。`boxp` GitHub organization membership を allow policy にする |
| Tunnel token | `boxp/arch` Terraform が SSM SecureString `hermes-agent-tunnel-token` に保存し、lolice 側 ExternalSecret が `hermes-agent-cloudflared-secret` へ同期 |
| Origin | `http://127.0.0.1:9119` inside the Hermes Pod |
| Origin Host header | `127.0.0.1:9119` に固定し、Hermes dashboard の loopback Host header 防御を満たす |
| Hermes API server | 引き続き `API_SERVER_ENABLED=false`。Web UI と OpenAI-compatible API server は別物として扱う |

## Runtime And Image Plan

Hermes Agent は公式 container image `docker.io/nousresearch/hermes-agent:latest` を採用する。公式 image は `/opt/data` を mutable state として扱い、`gateway run` を s6 supervision 下で実行する。Pod は Hermes image と ImageUpdater の platform を安定させるため `kubernetes.io/arch: amd64` に固定する。

Image rollout は Argo CD Image Updater で管理する。Hermes 公式 image は Docker Hub `latest` を digest strategy で追跡し、Obsidian headless 用の Node image は `node:22-bookworm-slim` を digest strategy で追跡する。公式 image で不足する system tool が出た場合だけ、`boxp/arch` で派生 image を作る。

Obsidian headless は `node:22-bookworm-slim` の initContainer で `npm install -g obsidian-headless@0.0.12` を実行し、install 先の `emptyDir` を `obsidian-sync` sidecar に read-only mount して使う。Pod 再起動時に npm の latest が変わらないよう、確認済み version を manifest に固定する。Hermes は専用 Longhorn PVC を持つため、初回起動時点では `~/.config/obsidian-headless` の認証状態は codex-workspace から自動継承されない。初回だけ Hermes Pod の `/home/boxp` に Obsidian headless の config を作成または移行し、その後は同じ PVC 上で継続利用する。

Hermes container と `obsidian-sync` sidecar は同じ PVC を `/opt/data` と `/home/boxp` として共有するため、実行 UID/GID を `1000:10000` に揃える。Hermes 公式 image には `HERMES_UID=1000` / `HERMES_GID=10000` を渡して runtime user を remap し、`obsidian-sync` も `runAsUser: 1000` / `runAsGroup: 10000` で実行する。初期化 container は共有 directory を `1000:10000`、mode `2775` に整え、sidecar は `umask 0002` で同期ファイルの group write を維持する。旧 UID `10000` で作られた既存 PVC にも対応するため、marker file がない初回だけ `/opt/data` 全体を `1000:10000` へ移行する。

起動コマンドの候補:

```sh
hermes gateway run
```

Kubernetes manifest では Docker image の entrypoint を維持し、container args に `gateway run` を渡す。API server は `API_SERVER_ENABLED=false` で無効化し、port 8642 の Service と固定 API key は作らない。HTTP health が必要になった場合は、API server を ExternalSecret 管理の key と限定 NetworkPolicy 付きで別途有効化する。

## Hermes Config

`/opt/data/config.yaml` を ConfigMap から PVC に初回だけ bootstrap する。公式 image は `/opt/data` を mutable state として扱い、起動時に config migration を行うため、既存の `/opt/data/config.yaml` は init container で上書きしない。local LLM は Hermes の first-class `custom` provider として扱う。

```yaml
model:
  provider: custom
  default: gemma4-26b-vision
  model: gemma4-26b-vision
  base_url: http://llama-server.local-llm.svc.cluster.local:8080/v1
  api_key: local-llm
  supports_vision: true
  context_length: 262144

display:
  tool_progress: "new"

tool_loop_guardrails:
  hard_stop_enabled: true
  hard_stop_after:
    exact_failure: 5
    idempotent_no_progress: 5
```

`local-llm` は現在 bearer token を強制していないため、実質的な認証 Secret は必須ではない。OpenAI-compatible client が non-empty key を要求する経路に備え、config には dummy key として `local-llm` を入れるが、Kubernetes Secret は作らない。

```env
HERMES_UID=1000
HERMES_GID=10000
LOCAL_LLM_BASE_URL=http://llama-server.local-llm.svc.cluster.local:8080/v1
LOCAL_LLM_MODEL=gemma4-26b-vision
API_SERVER_ENABLED=false
HERMES_DASHBOARD=true
HERMES_DASHBOARD_HOST=127.0.0.1
HERMES_DASHBOARD_PORT=9119
HERMES_DASHBOARD_PUBLIC_URL=https://hermes-agent.b0xp.io
HOME=/opt/data
OBSIDIAN_VAULT_PATH=/home/boxp/Documents/obsidian-headless/BOXP
```

Secret に入れる候補:

| Key | 用途 | 管理方針 |
| --- | --- | --- |
| `LOCAL_LLM_API_KEY` | local LLM が bearer を要求する場合のみ | ExternalSecret。認証不要のままなら作らない |
| `TELEGRAM_BOT_TOKEN` | Telegram gateway を使う場合 | ExternalSecret 第一候補 |
| `DISCORD_TOKEN` | Discord gateway を使う場合 | ExternalSecret 第一候補 |
| `SLACK_BOT_TOKEN` | Slack gateway を使う場合 | ExternalSecret 第一候補 |
| `HERMES_API_KEY` | Hermes API server を公開する場合 | ExternalSecret 第一候補 |
| `API_SERVER_KEY` | Hermes API server の OpenAI-compatible API 認証 | API server を有効化する場合のみ ExternalSecret で管理 |
| `TUNNEL_TOKEN` | Cloudflare Tunnel を起動する token | `hermes-agent-tunnel-token` を ExternalSecret で同期 |

non-secret:

| Key | 値 |
| --- | --- |
| `LOCAL_LLM_BASE_URL` | `http://llama-server.local-llm.svc.cluster.local:8080/v1` |
| `LOCAL_LLM_MODEL` | `gemma4-26b-vision` |
| `API_SERVER_ENABLED` | `false` |
| `HERMES_DASHBOARD` | `true` |
| `HERMES_DASHBOARD_HOST` | `127.0.0.1` |
| `HERMES_DASHBOARD_PORT` | `9119` |
| `HERMES_DASHBOARD_PUBLIC_URL` | `https://hermes-agent.b0xp.io` |
| `HERMES_UID` | `1000` |
| `HERMES_GID` | `10000` |
| `HOME` | `/opt/data` |
| `OBSIDIAN_VAULT_PATH` | `/home/boxp/Documents/obsidian-headless/BOXP` |

## Backend Connection Conditions

| Item | Decision |
| --- | --- |
| Endpoint | `http://llama-server.local-llm.svc.cluster.local:8080/v1` |
| Protocol | OpenAI-compatible chat completions |
| Text API | `POST /v1/chat/completions` |
| Models API | `GET /v1/models` |
| Auth | 現行 cluster 内では認証なし。Hermes config には dummy bearer `local-llm` を置く |
| Model ID | `gemma4-26b-vision` |
| Vision support | OpenAI chat completions 互換の `image_url` parts を使う。Hermes custom provider では `supports_vision: true` を明示する |
| Context | `262144` |
| Max output target | `4096` tokens |
| GPU | Hermes Pod は GPU 不要。推論は `local-llm` 側の GPU worker |

## PVC Plan

Hermes Agent の永続化対象は `~/.hermes` を中心に扱う。モデル本体は `local-llm` backend 側の hostPath `/var/lib/local-llm/models` の責務であり、Hermes PVC には置かない。

| Path | Data | Persistence | Initial Size | Notes |
| --- | --- | --- | --- | --- |
| `/opt/data` | config, auth store, sessions, memory, skills, gateway state, logs | Longhorn PVC | 10Gi | 公式 Docker image の mutable state path |
| `/home/boxp/Documents/obsidian-headless/BOXP` | Obsidian vault | same Longhorn PVC | `/opt/data` と共有 | `obsidian-sync` sidecar が `ob sync --continuous` で維持 |
| `/opt/data/workspace` | Hermes が作業生成物を置く場合 | Longhorn PVC または別 PVC | 10Gi 追加候補 | 初期は `/opt/data` に寄せ、必要なら分離 |
| `/tmp` | runtime cache | `emptyDir` | sizeLimit 2Gi | 再起動で消えてよい |
| logs | stdout/stderr | PVC 不要 | なし | Loki / kubectl logs 前提 |
| model weights | GGUF / mmproj | Hermes PVC 不要 | なし | `local-llm` hostPath 管理 |

推奨 PVC:

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: hermes-agent-data
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: longhorn
  resources:
    requests:
      storage: 10Gi
```

拡張方針:

- 70% 使用を継続的に超えたら 20Gi へ拡張する。
- 生成物や checkout が増える運用にする場合は `/workspace` を別 PVC 20Gi で分離する。
- Obsidian vault と Hermes state を同じ PVC に置くため、70% 閾値は vault 増加も含めて見る。
- backup は Longhorn backup を有効化する。memory/session や Obsidian vault を失うと agent の継続性が落ちるため、最低でも日次 backup を検討する。

## NetworkPolicy Plan

`hermes-agent` namespace は default deny とし、必要な通信だけ許可する。

Egress:

- kube-dns UDP/TCP 53
- `local-llm` namespace の `app == 'llama-server'` TCP 8080
- Obsidian sync / git / messaging gateway が必要とする TCP 22/80/443
- Obsidian sync が必要とする UDP 2408
- cloudflared tunnel が必要とする TCP 443
- package update や one-shot setup を Pod 内で実行する場合のみ GitHub / installer 向け HTTPS。通常運用 image では不要化する

Ingress:

- Kubernetes Service 経由の ingress はなし
- Web UI は同一 Pod 内の cloudflared sidecar から loopback origin へ転送するため、Pod への ingress allow は不要
- messaging gateway が polling 型のみなら原則不要
- webhook 型や API server / web dashboard を使う場合のみ、Cloudflare tunnel または private Service から限定許可
- API server を有効化する場合は固定 key を Git 管理せず、ExternalSecret の `API_SERVER_KEY` と送信元 Pod selector まで絞った NetworkPolicy を同時に入れる

現時点の `local-llm` namespace には ingress default deny がないため、Hermes 接続に必須なのは `hermes-agent` 側 egress allow である。`local-llm` 側に default deny を入れる場合は LAN LoadBalancer、monitoring、kubelet probe の既存経路も同時に扱う必要があるため、今回は破壊的な local-llm ingress policy は追加しない。後続で導入する場合は、少なくとも次を許可する。

- `hermes-agent` namespace の `app == 'hermes-agent'` から TCP 8080
- 既存 `codex-workspace` namespace の `app == 'codex-workspace'` から TCP 8080
- `monitoring` namespace から Envoy metrics TCP 9901
- LAN VIP 用の `192.168.10.0/24` から TCP 8080
- kubelet probe / node 経由の health check に必要な経路

## Minimal Verification

### 1. Backend direct health

```sh
kubectl -n hermes-agent run hermes-llm-smoke \
  --rm -it --restart=Never \
  --image=curlimages/curl:8.16.0 -- \
  curl -sS http://llama-server.local-llm.svc.cluster.local:8080/health
```

成功条件: HTTP 200 で `{"status":"ok"}` 相当が返る。

### 2. Backend model list

```sh
kubectl -n hermes-agent run hermes-llm-models \
  --rm -it --restart=Never \
  --image=curlimages/curl:8.16.0 -- \
  curl -sS http://llama-server.local-llm.svc.cluster.local:8080/v1/models
```

成功条件: response に `gemma4-26b-vision` が含まれる。

### 3. Backend text smoke

```sh
kubectl -n hermes-agent run hermes-llm-text \
  --rm -it --restart=Never \
  --image=curlimages/curl:8.16.0 -- \
  sh -c 'curl -sS http://llama-server.local-llm.svc.cluster.local:8080/v1/chat/completions \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer local-llm" \
    -d '"'"'{
      "model": "gemma4-26b-vision",
      "messages": [{"role": "user", "content": "日本語でOKとだけ返して"}],
      "max_tokens": 16
    }'"'"''
```

成功条件: HTTP 200 で assistant message が返る。日本語出力が壊れていないこと。

### 4. Backend vision smoke

OpenAI-compatible chat completions の `image_url` parts で base64 PNG を送る。

```sh
PNG_B64="$(base64 -w0 testdata/red.png)"
kubectl -n hermes-agent run hermes-llm-vision \
  --rm -it --restart=Never \
  --image=curlimages/curl:8.16.0 -- \
  sh -c "curl -sS http://llama-server.local-llm.svc.cluster.local:8080/v1/chat/completions \
    -H 'Content-Type: application/json' \
    -H 'Authorization: Bearer local-llm' \
    -d '{
      \"model\": \"gemma4-26b-vision\",
      \"messages\": [{
        \"role\": \"user\",
        \"content\": [
          {\"type\": \"text\", \"text\": \"画像の主な色を日本語で1語だけ答えて\"},
          {\"type\": \"image_url\", \"image_url\": {\"url\": \"data:image/png;base64,'\"$PNG_B64\"'\"}}
        ]
      }],
      \"max_tokens\": 32
    }'"
```

成功条件: HTTP 200 で `赤` など画像内容に即した回答が返る。

### 5. Obsidian headless smoke

Hermes Pod 起動後に Obsidian vault が同期されていることを確認する。新規 PVC の初回起動で `~/.config/obsidian-headless` が存在しない場合は、まず `obsidian-sync` container で Obsidian headless の認証を済ませるか、既存 codex-workspace PVC から同設定を手動移行する。

```sh
kubectl -n hermes-agent exec deploy/hermes-agent -c obsidian-sync -- \
  test -d /home/boxp/.config/obsidian-headless
kubectl -n hermes-agent exec deploy/hermes-agent -c obsidian-sync -- \
  test -d /home/boxp/Documents/obsidian-headless/BOXP
kubectl -n hermes-agent logs deploy/hermes-agent -c obsidian-sync --tail=100
kubectl -n hermes-agent exec deploy/hermes-agent -c hermes-agent -- \
  test -d /home/boxp/Documents/obsidian-headless/BOXP
```

成功条件: `obsidian-sync` が継続起動しており、Hermes container からも同じ vault path を参照できる。config 未作成で `ob sync` が認証エラーを出す場合は manifest ではなく初回 bootstrap 未完了として扱う。

### 6. Hermes direct smoke

Hermes Pod 起動後に exec で確認する。

```sh
kubectl -n hermes-agent exec deploy/hermes-agent -- hermes doctor
kubectl -n hermes-agent exec -it deploy/hermes-agent -- \
  hermes chat --model gemma4-26b-vision -q "日本語でOKとだけ返して"
```

成功条件: `hermes doctor` に provider/config の致命的警告がなく、chat が local LLM から応答する。

### 7. Web UI / Cloudflare Access smoke

Cloudflare Terraform PR apply と Argo CD sync 後に確認する。

```sh
kubectl -n hermes-agent logs deploy/hermes-agent -c cloudflared --tail=100
kubectl -n hermes-agent exec deploy/hermes-agent -c hermes-agent -- \
  sh -lc 'curl -sS -H "Host: 127.0.0.1:9119" http://127.0.0.1:9119/api/status'
curl -I https://hermes-agent.b0xp.io
```

成功条件:

- `cloudflared` container が `/ready` で healthy。
- Pod 内 loopback の `/api/status` が HTTP 200。
- 未認証ブラウザまたは `curl -I` では Cloudflare Access の login redirect / Access response になり、origin が直接公開されない。
- `boxp` GitHub organization member として認証後に Hermes dashboard が表示される。
- `boxp` GitHub organization に属さない GitHub account は Cloudflare Access で拒否される。

## Operations

通常操作:

```sh
kubectl -n hermes-agent get pod,pvc
kubectl -n hermes-agent logs deploy/hermes-agent -c hermes-agent -f
kubectl -n hermes-agent logs deploy/hermes-agent -c obsidian-sync -f
kubectl -n hermes-agent logs deploy/hermes-agent -c cloudflared -f
kubectl -n hermes-agent rollout restart deploy/hermes-agent
kubectl -n hermes-agent rollout status deploy/hermes-agent
```

LLM 側確認:

```sh
kubectl -n local-llm get pod -o wide
kubectl -n local-llm logs deploy/llama-server -c llama-server --tail=200
kubectl -n local-llm logs deploy/llama-server -c envoy --tail=200
kubectl -n local-llm port-forward svc/llama-server 8080:8080
curl -sS http://127.0.0.1:8080/health
curl -sS http://127.0.0.1:8080/v1/models
```

Health check 方針:

- Hermes に HTTP health endpoint がある場合は readiness/liveness に使う。
- HTTP health がない場合は liveness を弱くし、startupProbe/readiness は `hermes doctor` 相当の exec probe を検討する。
- gateway polling 型では外部 inbound がないため、起動済みかどうかは logs と platform API 接続で確認する。

Resource request 初期値:

| Container | CPU request | Memory request | Limit |
| --- | --- | --- | --- |
| `hermes-agent` | 500m | 1Gi | CPU 2, memory 4Gi |
| `obsidian-sync` | 50m | 128Mi | CPU 500m, memory 512Mi |
| `cloudflared` | 20m | 64Mi | CPU 200m, memory 256Mi |

Hermes Pod は GPU 不要。LLM 推論は `local-llm` Deployment が担当する。

## Failure Triage

| Symptom | First checks | Likely cause | Action |
| --- | --- | --- | --- |
| Hermes Pod が起動しない | `kubectl logs`, `describe pod` | image 依存不足、HOME/PVC 権限、config syntax | image build と securityContext、PVC owner を確認 |
| `Unknown provider` | `hermes doctor`, mounted config | custom provider config の schema 不一致 | `config.yaml` を Hermes version に合わせる |
| `/v1/models` に到達しない | debug Pod から curl、NetworkPolicy | DNS/egress deny、local-llm Service 不在 | DNS と `local-llm` egress TCP 8080 を確認 |
| `gemma4-26b-vision` load 失敗 | `local-llm` llama-server logs、`/v1/models` failed flag | GGUF/mmproj 不在、GPU memory、router child 起動失敗 | `golyat-4:/var/lib/local-llm/models` と resource を確認 |
| text は動くが vision が失敗 | request body、llama-server logs | `supports_vision` 未設定、image_url 形式違い、mmproj load 失敗 | Hermes config と backend vision smoke を確認 |
| gateway が platform に接続しない | Hermes logs、Secret mount、platform token | token 不正、Secret 未同期、outbound 443 deny | ExternalSecret と NetworkPolicy を確認 |
| memory/session が消える | PVC mount、`/opt/data` | `/opt/data` が emptyDir/rootfs に書かれている | PVC mount path を修正 |
| Obsidian vault が見えない | `obsidian-sync` logs、`OBSIDIAN_VAULT_PATH`、PVC mount | sidecar 停止、sync 認証不備、PVC 権限 | `ob sync` logs と `/home/boxp` mount を確認 |
| Obsidian sync が認証エラーになる | `test -d /home/boxp/.config/obsidian-headless`, `obsidian-sync` logs | Hermes 専用 PVC に headless config が未作成 | 初回だけ `obsidian-sync` container で認証するか codex-workspace の config を移行 |
| `hermes-agent.b0xp.io` が 502 | `cloudflared` logs、Pod 内 `curl`、Terraform tunnel config | tunnel token 未同期、origin が起動していない、Host header 防御 | ExternalSecret、`HERMES_DASHBOARD=true`、origin host header `127.0.0.1:9119` を確認 |
| Web UI が Cloudflare Access を通らない | Cloudflare Access application/policy、DNS proxied | Access policy 未作成、DNS が tunnel CNAME でない | `boxp/arch` の `terraform/cloudflare/b0xp.io/hermes-agent` plan/apply を確認 |
| local LLM 応答が遅い | Grafana local LLM dashboard、Envoy latency、GPU utilization | モデル cold load、GPU worker 負荷、context 過大 | `local-llm` の model load 状態と concurrency を確認 |

## Implementation PR Units

### PR 1: Hermes Agent runtime image

Repository: `boxp/arch`

Status: 今回は公式 image を採用したため不要。

想定変更:

- Hermes Agent 用 container image build
- base image / Python 3.11 / Node.js / uv / git / ripgrep / ffmpeg
- non-root `hermes` user
- smoke command in CI if feasible

公式 image を採用できる場合、この PR は不要。

### PR 2: lolice GitOps manifests

Repository: `boxp/lolice`

Status: この PR で実装済み。

想定変更:

- `argoproj/hermes-agent/namespace.yaml`
- `argoproj/hermes-agent/argocd-application.yaml`
- `argoproj/hermes-agent/kustomization.yaml`
- `argoproj/hermes-agent/deployment.yaml`
- `argoproj/hermes-agent/configmap.yaml`
- `argoproj/hermes-agent/pvc.yaml`
- `argoproj/hermes-agent/external-secret-cloudflared.yaml`
- `argoproj/hermes-agent/networkpolicy.yaml`
- `argoproj/argocd-image-updater/imageupdaters/hermes-agent.yaml`
- `argoproj/kustomization.yaml` に `hermes-agent/argocd-application.yaml` を追加
- `local-llm` 側 ingress policy は今回の PR では未追加。default deny 導入時の後続 PR で追加する
- `argoproj/argocd-image-updater/imageupdaters/hermes-agent.yaml`

Validation:

```sh
kubectl kustomize argoproj/hermes-agent
kubectl kustomize argoproj/argocd-image-updater/imageupdaters
kubectl kustomize argoproj
kubectl apply --dry-run=server -k argoproj/hermes-agent
```

今回の local validation:

```sh
kubectl kustomize argoproj/hermes-agent
kubectl kustomize argoproj/local-llm
kubectl kustomize argoproj/argocd-image-updater/imageupdaters
kubectl kustomize argoproj
```

`kubectl apply --dry-run=client -k argoproj/hermes-agent` は standard resources の dry-run までは成功したが、Calico CRD discovery 待ちで止まったため中断した。server-side dry-run は runner の cluster RBAC / API 到達性に依存するため、Argo CD sync 前に管理者環境で実施する。

### PR 3: Cloudflare Web UI exposure

Repository: `boxp/arch`

Status: この run で別 PR として実装。

想定変更:

- `terraform/cloudflare/b0xp.io/hermes-agent/`
- `cloudflare_tunnel` / `cloudflare_tunnel_config`
- `cloudflare_record` for `hermes-agent.b0xp.io`
- `cloudflare_access_application` と GitHub IdP + `boxp` organization allow policy
- `aws_ssm_parameter.hermes_agent_tunnel_token`

Validation:

```sh
cd terraform/cloudflare/b0xp.io/hermes-agent
terraform fmt
terraform init -backend=false
terraform validate
```

## Open Decisions After PR 2

- Web UI 以外の常時稼働入口を何にするか: Telegram / Discord / Slack / API server / Desktop remote gateway。
- messaging/API secrets を ExternalSecret 化するか。
- API server を有効化する場合の送信元 selector と ExternalSecret key 名。
- Hermes の公式 image を digest pin するか、Argo CD Image Updater で追跡するか。
- `local-llm` の bearer 認証を今後有効化するか。
- `gemma4-26b-vision` を常用 default にするか、text-only の `gemma4-26b` を primary、vision は必要時切替にするか。

## Acceptance Mapping

- 実行環境、起動コマンド、設定ファイル、secret/env var: `Runtime And Image Plan`, `Hermes Config`
- `gemma4-26b-vision` 提供方式: `Backend Connection Conditions`
- 全体アーキテクチャ: `Target Architecture`
- PVC 要否・用途・サイズ: `PVC Plan`
- backend 疎通確認: `Minimal Verification`
- text / vision 最小確認: `Minimal Verification`
- 永続運用方式: `Target Architecture`, `Operations`
- 失敗時切り分け: `Failure Triage`
- 後続変更リポジトリ・ファイル・PR 単位: `Implementation PR Units`

## Notes

- 2026-07-05: BOXP-52 の設計計画として作成。現行 `boxp/lolice` の `local-llm` 実装では `gemma4-26b-vision` が OpenAI-compatible endpoint で公開済みのため、Hermes 側は custom provider で接続する方針にした。
- 2026-07-05: 計画を実装へ進め、`argoproj/hermes-agent` に公式 `nousresearch/hermes-agent` image ベースの Deployment / PVC 10Gi / ConfigMap / Calico NetworkPolicy / Argo CD Application / ImageUpdater を追加した。API server は固定 key で公開せず無効化し、Obsidian headless は `codex-workspace` と同じ `ob sync --continuous` sidecar を追加した。custom provider は vision 入力を native に送るため `supports_vision: true` を明示した。`local-llm` 側は現時点で ingress default deny がなく、LAN VIP や kubelet probe への影響が大きいため、この PR では Hermes 側 egress allow のみに留めた。
- 2026-07-05: Codex review の指摘を受け、Hermes と Obsidian sidecar が同じ vault PVC を安全に更新できるように UID/GID を `1000:10000` へ統一した。Hermes 公式 image の `HERMES_UID` / `HERMES_GID` remap を使い、bootstrap directory は setgid `2775`、`obsidian-sync` は `umask 0002` で group write を維持する。既存 PVC が旧 Hermes UID `10000` のファイルを持つ場合も壊れないように、initContainer で一回限りの ownership migration を行う。
- 2026-07-05: Obsidian headless は専用 PVC では認証 config が自動継承されないため、初回 bootstrap と config 確認を smoke / failure triage に追記した。
- 2026-07-05: PR review コメントを受け、Obsidian sync のためだけに `ghcr.io/boxp/arch/codex-workspace` を使う構成をやめた。`node:22-bookworm-slim` の initContainer で `obsidian-headless@0.0.12` を install し、同 image の sidecar が install 済み `ob` を使って sync する構成へ変更した。
- 2026-07-05: Web UI を `hermes-agent.b0xp.io` で Cloudflare 公開する構成を追加した。Hermes dashboard は `127.0.0.1:9119` にだけ bind し、同一 Pod の `cloudflared` sidecar から loopback origin へ転送する。Cloudflare Access は GitHub IdP 必須かつ `boxp` organization member のみ許可し、tunnel token は `boxp/arch` Terraform の SSM SecureString `hermes-agent-tunnel-token` から lolice ExternalSecret で同期する。
