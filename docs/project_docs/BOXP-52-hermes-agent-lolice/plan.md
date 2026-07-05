# BOXP-52: lolice cluster で Hermes Agent を常時稼働する計画

## Goal

lolice cluster 上に NousResearch Hermes Agent を常時稼働させ、外部 hosted LLM API ではなく既存の local LLM `gemma4-26b-vision` を backend として使う。

このチケットでは実装 manifest は追加せず、後続実装 PR に進められる粒度で、実行環境、設定、Secret/PVC、NetworkPolicy、疎通確認、運用・障害切り分けを確定する。

## Confirmed Facts

- Hermes Agent の入手元は `NousResearch/hermes-agent`。
- Hermes Agent は Linux/macOS/WSL2 向けに `curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash` で CLI を導入できる。
- Hermes Agent は `hermes model` / `config.yaml` で custom endpoint を設定でき、OpenAI-compatible な `/v1/chat/completions` endpoint に接続できる。
- Hermes Agent は self-hosted endpoint / vLLM / Ollama などを custom provider として扱える。
- Hermes Agent の設定と永続状態は通常 `~/.hermes/` 配下に置かれる。
- Gateway は foreground の `hermes gateway`、user service の `hermes gateway install`、Linux system service の `sudo hermes gateway install --system` で起動できる。
- lolice の local LLM は `argoproj/local-llm` で GitOps 管理されている。
- lolice cluster 内の OpenAI-compatible endpoint は `http://llama-server.local-llm.svc.cluster.local:8080/v1`。
- `argoproj/local-llm/models-config.yaml` では `gemma4-26b-vision` がモデル ID として定義済み。
- `gemma4-26b-vision` は `Gemma4-26B-A4B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M.gguf` と `mmproj-Gemma4-26B-A4B-QAT-Uncensored-HauhauCS-Balanced-BF16.gguf` を使う vision model として定義済み。
- `argoproj/codex-workspace/configmap.yaml` の pi agent 設定では `gemma4-26b-vision` が `input: ["text", "image"]`、`contextWindow: 262144`、`maxTokens: 4096` として公開済み。
- `argoproj/codex-workspace/networkpolicy.yaml` には `codex-workspace` から `local-llm` namespace の `app == 'llama-server'` TCP 8080 への egress 許可例がある。

## External References

- Hermes Agent repository: https://github.com/NousResearch/hermes-agent
- Hermes Agent install docs: https://hermes-agent.nousresearch.com/docs/getting-started/installation
- Hermes Agent provider docs: https://hermes-agent.nousresearch.com/docs/integrations/providers
- Hermes Agent messaging gateway docs: https://hermes-agent.nousresearch.com/docs/user-guide/messaging/
- Hermes Agent Docker docs: https://hermes-agent.nousresearch.com/docs/user-guide/docker

## Target Architecture

第一候補は Kubernetes Deployment + Argo CD/GitOps 管理とする。systemd、tmux、手動 Pod は bootstrap や一時検証だけに限定する。

```text
user / messaging platform / optional web UI
  -> Hermes Agent gateway Pod
  -> Hermes Agent runtime in same Pod
  -> custom OpenAI-compatible provider
  -> http://llama-server.local-llm.svc.cluster.local:8080/v1
  -> local-llm Deployment on GPU worker
  -> gemma4-26b-vision
```

Kubernetes resources:

| Resource | 方針 |
| --- | --- |
| Repository | `boxp/lolice` |
| Directory | `argoproj/hermes-agent/` |
| Argo CD Application | `argoproj/hermes-agent/application.yaml` |
| Namespace | `hermes-agent` |
| Workload | `Deployment/hermes-agent`, replicas 1 |
| Service | cluster 内利用のみなら不要。API server / web UI / gateway health を公開する場合は ClusterIP |
| PVC | Longhorn PVC 1 個を `/home/hermes/.hermes` に mount |
| ConfigMap | `config.yaml`、non-secret の model/provider/runtime 設定 |
| Secret / ExternalSecret | messaging token、任意の Hermes API key、必要なら local LLM bearer token |
| NetworkPolicy | default deny 前提。DNS、local-llm:8080、必要な outbound HTTPS、必要な inbound のみ許可 |
| Observability | stdout/stderr logs。必要なら ServiceMonitor は後続で追加 |

## Runtime And Image Plan

Hermes Agent は公式 container image の有無が安定運用の要になる。後続実装では次の順で採用する。

1. 公式または upstream Helm chart が提供する image が利用可能なら、その image を pin して `argoproj/hermes-agent` で使う。
2. 公式 image がなければ `boxp/arch` に `ghcr.io/boxp/arch/hermes-agent` image build を追加する。
3. image は Linux amd64、Python 3.11、Node.js、uv、git、ripgrep、ffmpeg を含む。Hermes installer が導入する依存に合わせる。
4. root ではなく `hermes` user で実行し、`HOME=/home/hermes`、`HERMES_HOME=/home/hermes/.hermes` を固定する。

起動コマンドの候補:

```sh
hermes gateway
```

または、gateway を使わず API server / CLI のみを常時化する場合は、後続調査で Hermes の server mode の正式 command を確認してから manifest 化する。現時点の常時運用単位は messaging gateway とする。

## Hermes Config

`~/.hermes/config.yaml` 相当を ConfigMap から配置する。local LLM は named custom provider として扱う。

```yaml
custom_providers:
  - name: lolice-local-llm
    base_url: http://llama-server.local-llm.svc.cluster.local:8080/v1
    key_env: LOCAL_LLM_API_KEY
    api_mode: chat_completions
    model: gemma4-26b-vision
    models:
      gemma4-26b-vision:
        context_length: 262144
        supports_vision: true

model:
  provider: custom:lolice-local-llm
  default: gemma4-26b-vision
  base_url: http://llama-server.local-llm.svc.cluster.local:8080/v1
  context_length: 262144
  supports_vision: true
```

`local-llm` は現在 bearer token を強制していないため、実質的な認証 Secret は必須ではない。ただし Hermes の custom provider から確実に `Authorization` を付与できるように、`key_env: LOCAL_LLM_API_KEY` を設定し、dummy key として `local-llm` を Secret から渡す。

```env
HERMES_HOME=/home/hermes/.hermes
HOME=/home/hermes
LOCAL_LLM_BASE_URL=http://llama-server.local-llm.svc.cluster.local:8080/v1
LOCAL_LLM_MODEL=gemma4-26b-vision
LOCAL_LLM_API_KEY=local-llm
```

Secret に入れる候補:

| Key | 用途 | 管理方針 |
| --- | --- | --- |
| `LOCAL_LLM_API_KEY` | local LLM が bearer を要求する場合のみ | Kubernetes Secret。認証不要のままなら dummy |
| `TELEGRAM_BOT_TOKEN` | Telegram gateway を使う場合 | ExternalSecret 第一候補 |
| `DISCORD_TOKEN` | Discord gateway を使う場合 | ExternalSecret 第一候補 |
| `SLACK_BOT_TOKEN` | Slack gateway を使う場合 | ExternalSecret 第一候補 |
| `HERMES_API_KEY` | Hermes API server を公開する場合 | ExternalSecret 第一候補 |

non-secret:

| Key | 値 |
| --- | --- |
| `LOCAL_LLM_BASE_URL` | `http://llama-server.local-llm.svc.cluster.local:8080/v1` |
| `LOCAL_LLM_MODEL` | `gemma4-26b-vision` |
| `HERMES_HOME` | `/home/hermes/.hermes` |

## Backend Connection Conditions

| Item | Decision |
| --- | --- |
| Endpoint | `http://llama-server.local-llm.svc.cluster.local:8080/v1` |
| Protocol | OpenAI-compatible chat completions |
| Text API | `POST /v1/chat/completions` |
| Models API | `GET /v1/models` |
| Auth | 現行 cluster 内では認証なし。Hermes には dummy bearer `local-llm` を渡せるようにする |
| Model ID | `gemma4-26b-vision` |
| Vision support | OpenAI chat completions 互換の `image_url` parts を使う |
| Context | `262144` |
| Max output target | `4096` tokens |
| GPU | Hermes Pod は GPU 不要。推論は `local-llm` 側の GPU worker |

## PVC Plan

Hermes Agent の永続化対象は `~/.hermes` を中心に扱う。モデル本体は `local-llm` backend 側の hostPath `/var/lib/local-llm/models` の責務であり、Hermes PVC には置かない。

| Path | Data | Persistence | Initial Size | Notes |
| --- | --- | --- | --- | --- |
| `/home/hermes/.hermes` | config, auth store, sessions, memory, skills, gateway state | Longhorn PVC | 10Gi | 最初の本番 PVC。増加傾向確認後に拡張 |
| `/workspace` | Hermes が作業生成物を置く場合 | Longhorn PVC または同一 PVC subdir | 10Gi 追加候補 | 初期は `.hermes/workspace` に寄せ、必要なら分離 |
| `/tmp` | runtime cache | `emptyDir` | sizeLimit 2Gi | 再起動で消えてよい |
| logs | stdout/stderr | PVC 不要 | なし | Loki / kubectl logs 前提 |
| model weights | GGUF / mmproj | Hermes PVC 不要 | なし | `local-llm` hostPath 管理 |

推奨 PVC:

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: hermes-agent-home
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
- backup は Longhorn backup を有効化する。memory/session を失うと agent の継続性が落ちるため、最低でも日次 backup を検討する。

## NetworkPolicy Plan

`hermes-agent` namespace は default deny とし、必要な通信だけ許可する。

Egress:

- kube-dns UDP/TCP 53
- `local-llm` namespace の `app == 'llama-server'` TCP 8080
- messaging gateway が必要とする外部 HTTPS TCP 443
- package update や one-shot setup を Pod 内で実行する場合のみ GitHub / installer 向け HTTPS。通常運用 image では不要化する

Ingress:

- messaging gateway が polling 型のみなら原則不要
- webhook 型や API server / web dashboard を使う場合のみ、Cloudflare tunnel または private Service から限定許可
- health endpoint を Service として公開する場合は monitoring namespace から限定許可

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

### 5. Hermes direct smoke

Hermes Pod 起動後に exec で確認する。

```sh
kubectl -n hermes-agent exec deploy/hermes-agent -- hermes doctor
kubectl -n hermes-agent exec -it deploy/hermes-agent -- hermes chat \
  --provider custom:lolice-local-llm \
  --model gemma4-26b-vision \
  -q "日本語でOKとだけ返して"
```

成功条件: `hermes doctor` に provider/config の致命的警告がなく、chat が local LLM から応答する。

## Operations

通常操作:

```sh
kubectl -n hermes-agent get pod,pvc
kubectl -n hermes-agent logs deploy/hermes-agent -f
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
| `hermes-agent` | 500m | 1Gi | CPU limit なし、memory 4Gi |

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
| memory/session が消える | PVC mount、`HERMES_HOME` | `~/.hermes` が emptyDir/rootfs に書かれている | PVC mount path と env を修正 |
| local LLM 応答が遅い | Grafana local LLM dashboard、Envoy latency、GPU utilization | モデル cold load、GPU worker 負荷、context 過大 | `local-llm` の model load 状態と concurrency を確認 |

## Implementation PR Units

### PR 1: Hermes Agent runtime image

Repository: `boxp/arch`

想定変更:

- Hermes Agent 用 container image build
- base image / Python 3.11 / Node.js / uv / git / ripgrep / ffmpeg
- non-root `hermes` user
- smoke command in CI if feasible

公式 image を採用できる場合、この PR は不要。

### PR 2: lolice GitOps manifests

Repository: `boxp/lolice`

想定変更:

- `argoproj/hermes-agent/namespace.yaml`
- `argoproj/hermes-agent/application.yaml`
- `argoproj/hermes-agent/kustomization.yaml`
- `argoproj/hermes-agent/deployment.yaml`
- `argoproj/hermes-agent/configmap.yaml`
- `argoproj/hermes-agent/pvc.yaml`
- `argoproj/hermes-agent/networkpolicy.yaml`
- `argoproj/hermes-agent/external-secret.yaml` if messaging/API secrets are used
- `argoproj/kustomization.yaml` に `hermes-agent/application.yaml` を追加
- 必要なら `argoproj/argocd-image-updater/imageupdaters/hermes-agent.yaml`

Validation:

```sh
kubectl kustomize argoproj/hermes-agent
kubectl kustomize argoproj
kubectl apply --dry-run=server -k argoproj/hermes-agent
```

### PR 3: Optional ingress / UI / messaging integration

Repository: `boxp/lolice` and possibly `boxp/arch`

想定変更:

- Cloudflare Tunnel / WARP private route
- webhook 型 messaging を使う場合の Service / route
- dashboard/API を公開する場合の auth と NetworkPolicy

## Open Decisions Before PR 2

- 常時稼働させる入口を何にするか: Telegram / Discord / Slack / API server / Desktop remote gateway。
- Hermes Agent の公式 image または Helm chart を使うか、`boxp/arch` で image を作るか。
- Hermes の health endpoint / API server command の現行 version における正式 command。
- `hermes setup` を bootstrap Job で実行するか、`config.yaml` と Secret を GitOps で完全に生成するか。
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
