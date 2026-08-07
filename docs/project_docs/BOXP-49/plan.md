# BOXP-49: Chroma1-HD + ComfyUI feasibility and rollout plan

## 結論

Chroma1-HD + ComfyUI で条件付きで進める。`golyat-4` の Intel GPU は 1 枚だけなので、`local-llm/llama-server`、`stable-diffusion-webui`、`comfyui-chroma` は同時に GPU request できない。既存運用と同じく、画像生成時だけ `llama-server` または SD.Next を 0 replica にし、`comfyui-chroma` を 1 replica に上げる切替運用にする。

## 選定

- モデル: `lodestones/Chroma1-HD`
- ライセンス: Apache-2.0
- runtime: ComfyUI
- endpoint: `http://comfyui-chroma.stable-diffusion.svc.cluster.local:8188`
- pi agents / codex-workspace 接続: `COMFYUI_CHROMA_URL` 環境変数と Calico NetworkPolicy egress で許可
- llama-server 連携: ComfyUI Pod に `LLAMA_SERVER_BASE_URL=http://llama-server.local-llm.svc.cluster.local:8080/v1` を渡す。Chroma text-to-image 本体は ComfyUI workflow で実行し、prompt 補助や ComfyUI custom node 連携が必要な場合に既存 llama-server を参照する。

参照した一次情報:

- https://huggingface.co/lodestones/Chroma1-HD
- https://comfyanonymous.github.io/ComfyUI_examples/chroma/
- https://huggingface.co/lodestones/Chroma1-HD/blob/main/ComfyUI_Chroma1-HD_T2I-workflow.json

## モデル配置

既存 `stable-diffusion/sd-webui-data` PVC を共有し、ComfyUI 用のパスを `/data/comfyui` に分ける。

- Chroma checkpoint: `/data/comfyui/models/diffusion_models/Chroma1-HD.safetensors`
- text encoder: `/data/comfyui/models/text_encoders/t5xxl_fp8_e4m3fn_scaled.safetensors`
- VAE: `/data/comfyui/models/vae/ae.safetensors`
- workflow: `/data/comfyui/workflows/ComfyUI_Chroma1-HD_T2I-workflow.json`
- output: `/data/comfyui/output`
- cache / tmp: `golyat-4:/var/lib/comfyui-chroma/scratch`

初回は ComfyUI 公式例が memory 少なめ構成として推奨している `t5xxl_fp8_e4m3fn_scaled.safetensors` を使う。画質または prompt 理解で不足が出た場合だけ `t5xxl_fp16.safetensors` に切り替える。

注意: `ComfyUI_Chroma1-HD_T2I-workflow.json` の配布版は `UNETLoader` の default が低 VRAM 向け `Chroma1-HD-fp8_scaled_rev2.safetensors` になっている。今回の採用モデルを `Chroma1-HD.safetensors` に固定する場合は、smoke test 前に workflow の該当 widget を `Chroma1-HD.safetensors` へ差し替える。

## リソース判断

2026-07-05 実測:

- `golyat-4` allocatable: CPU 16, memory 約 93Gi, `gpu.intel.com/i915: 1`
- 現在の GPU request: `local-llm/llama-server` が `gpu.intel.com/i915: 1`
- `stable-diffusion-webui`: `replicas: 0`
- `/mnt/ssd`: 1.9T 中 304G 使用、約 1.6T 空き
- `golyat-4` host memory: 93Gi total / 61Gi available

Chroma1-HD 関連の想定保存量:

- Chroma1-HD checkpoint: 約 17.8GB
- T5 XXL fp8 scaled: 約 5.2GB
- T5 XXL fp16: 約 9.8GB
- FLUX VAE: 約 0.34GB
- workflow / metadata: 小さい
- cache / output: 生成頻度に依存するため初期は 50-100GB 程度を監視対象にする

PVC の実容量は 1848Gi として bind されており、モデル本体・cache・生成物の初期運用には十分余裕がある。重要な制約はストレージではなく 1 枚の Intel GPU とランタイム image の未検証部分。

## GitOps 変更

- `stable-diffusion` Application に `comfyui-chroma` Deployment / Service / NetworkPolicy / extra model paths ConfigMap を追加する。
- `comfyui-chroma` は初期 `replicas: 0`。merge だけでは GPU を奪わない。
- `priorityClassName: gpu-workload-high`、`nodeSelector: lolice.io/gpu-worker=true`、`gpu.intel.com/i915: "1"` を設定する。
- image は `ghcr.io/boxp/arch/comfyui-ipex:latest` を仮置きし、`stable-diffusion` の Argo CD Image Updater に digest tracking 対象として追加する。別途 `boxp/arch` 側で ComfyUI + Intel IPEX/XPU runtime image を build/publish する必要がある。
- `codex-workspace` から `comfyui-chroma:8188` への egress を許可し、workspace container に `COMFYUI_CHROMA_URL` を追加する。

## 起動停止手順

Chroma 利用開始:

```bash
kubectl -n local-llm scale deploy/llama-server --replicas=0
kubectl -n stable-diffusion scale deploy/stable-diffusion-webui --replicas=0
kubectl -n stable-diffusion scale deploy/comfyui-chroma --replicas=1
kubectl -n stable-diffusion rollout status deploy/comfyui-chroma --timeout=900s
```

通常 LLM 運用へ戻す:

```bash
kubectl -n stable-diffusion scale deploy/comfyui-chroma --replicas=0
kubectl -n local-llm scale deploy/llama-server --replicas=1
kubectl -n local-llm rollout status deploy/llama-server --timeout=900s
```

## モデル seed 手順

```bash
ssh boxp@192.168.10.107 'mkdir -p /mnt/ssd/stable-diffusion-webui-data/comfyui/models/{diffusion_models,text_encoders,vae} /mnt/ssd/stable-diffusion-webui-data/comfyui/{workflows,input,output}'

ssh boxp@192.168.10.107 'cd /mnt/ssd/stable-diffusion-webui-data/comfyui && \
  curl -L -o models/diffusion_models/Chroma1-HD.safetensors https://huggingface.co/lodestones/Chroma1-HD/resolve/main/Chroma1-HD.safetensors && \
  curl -L -o models/text_encoders/t5xxl_fp8_e4m3fn_scaled.safetensors https://huggingface.co/comfyanonymous/flux_text_encoders/resolve/main/t5xxl_fp8_e4m3fn_scaled.safetensors && \
  curl -L -o models/vae/ae.safetensors https://huggingface.co/Comfy-Org/Lumina_Image_2.0_Repackaged/resolve/main/split_files/vae/ae.safetensors && \
  curl -L -o workflows/ComfyUI_Chroma1-HD_T2I-workflow.json https://huggingface.co/lodestones/Chroma1-HD/resolve/main/ComfyUI_Chroma1-HD_T2I-workflow.json'
```

## smoke test

```bash
kubectl -n stable-diffusion port-forward svc/comfyui-chroma 18188:8188
curl -fsS http://127.0.0.1:18188/system_stats
```

ComfyUI API の最小 text-to-image smoke は、seed 済みの `ComfyUI_Chroma1-HD_T2I-workflow.json` の `UNETLoader` を `Chroma1-HD.safetensors` に、prompt node を固定 prompt に差し替えて `/prompt` へ POST し、`/history/{prompt_id}` と `/view` で生成物を確認する。実行前に workflow JSON の node id を確認してから payload を作る。

## この run の検証

- `kubectl kustomize argoproj/stable-diffusion` 成功。
- `kubectl kustomize argoproj/codex-workspace` 成功。
- `kubectl kustomize argoproj/argocd-image-updater` 成功。
- `kubectl apply --dry-run=server -k argoproj/stable-diffusion` は、この task-board ServiceAccount が `stable-diffusion` namespace の create/patch 権限を持たないため Forbidden。
- `kubectl apply --dry-run=server -k argoproj/codex-workspace` は、この task-board ServiceAccount が cluster-scope resource と `codex-workspace` namespace の patch 権限を持たないため Forbidden。
- `kubectl apply --dry-run=client` は API discovery/validation 待ちが長く、手動中断。render は成功済み。

## 未解決 / follow-up

- `ghcr.io/boxp/arch/comfyui-ipex:latest` image を `boxp/arch` に追加して publish する。
- Chroma1-HD workflow が Intel XPU/IPEX で完走するか、初回のみ手動 smoke test する。
- ComfyUI の prompt 補助で既存 `llama-server` を使う custom node / job wrapper を決める。
- 生成物 retention とバックアップ要否を決める。初期判断は「モデル本体は再取得可能、意図して保存した output だけ必要に応じてバックアップ」。
