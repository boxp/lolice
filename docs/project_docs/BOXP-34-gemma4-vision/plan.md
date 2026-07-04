# BOXP-34: local-llm gemma4-26b vision input

## Context

`local-llm` の `gemma4-26b` は `llama-server --models-preset /config/models.ini` で公開している。既存のモデル ID は pi agent から参照済みなので、検証用の別 ID は作らず、既存 `gemma4-26b` を画像入力対応として昇格する。

`/var/lib/local-llm/models` はこの作業環境から中身を確認できなかったため、現行ファイルの実機配置確認はデプロイ前手順に残す。公開配布元はファイル名から `HauhauCS/Gemma4-26B-A4B-QAT-Uncensored-HauhauCS-Balanced-MTP` と判断した。同 repo には現行本体 `Gemma4-26B-A4B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M.gguf` と、vision projector `mmproj-Gemma4-26B-A4B-QAT-Uncensored-HauhauCS-Balanced-BF16.gguf` が含まれる。

`llama.cpp` の multimodal docs では、`llama-server` は OpenAI-compatible `/chat/completions` で multimodal input を扱い、ローカル GGUF では `-m model.gguf --mmproj file.gguf` を指定する。server README の model presets では `.ini` の key は command-line arguments から leading dash を外した名前で書けるため、`models.ini` には `mmproj = /models/...gguf` を追加する。

## Design

- `argoproj/local-llm/models-config.yaml`
  - `[gemma4-26b]` に `mmproj = /models/mmproj-Gemma4-26B-A4B-QAT-Uncensored-HauhauCS-Balanced-BF16.gguf` を追加する。
  - `ctx-size = 262144`, `batch-size = 2048`, `ubatch-size = 1024` は維持する。
  - 画像トークン関連オプションは明示しない。llama.cpp 側の model/projector metadata と既定値に任せ、実機 smoke test で不足が出た場合のみ追加する。
- `argoproj/local-llm/deployment.yaml`
  - 起動 args は維持する。`--models-preset` 経由で `mmproj` が渡るため、router 全体の共通 args には追加しない。
  - `/var/lib/local-llm/models` の hostPath read-only mount は維持する。追加ファイルはホスト側へ事前配置する。
  - `models.ini` は `subPath` mount のため、`llama-server-models` ConfigMap 更新時に既存 Pod が再作成されるよう `configmap.reloader.stakater.com/reload` annotation を付ける。
  - `ghcr.io/boxp/arch/llama-sycl:latest` はデプロイ前に `llama-server --help` で `--mmproj` と `--models-preset` を確認する。
- `argoproj/local-llm/service.yaml` / `envoy-config.yaml`
  - Service は TCP/HTTP proxy のみなので変更不要。
  - Envoy は router filter のみで body 変換をせず、route timeout と max stream duration は `0s` なので長い vision 推論のための timeout 変更は不要。
  - base64 data URL が大きすぎる場合だけ `max_request_bytes` 相当の制限導入を検討する。初回 smoke test は小さい PNG/JPEG で行う。
- `argoproj/codex-workspace/configmap.yaml`
  - pi agent の `gemma4-26b` は既存 ID のまま `input` を `["text", "image"]` にする。

## Model Files

配置先は GPU worker node の `/var/lib/local-llm/models`。

| file | source | size | required |
| --- | --- | ---: | --- |
| `Gemma4-26B-A4B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M.gguf` | `HauhauCS/Gemma4-26B-A4B-QAT-Uncensored-HauhauCS-Balanced-MTP` | 16,796,015,520 bytes | yes |
| `mmproj-Gemma4-26B-A4B-QAT-Uncensored-HauhauCS-Balanced-BF16.gguf` | same | 1,194,827,776 bytes | yes |
| `mtp-gemma-4-26B-A4B-it.gguf` | same | 251,937,728 bytes | no |

MTP draft model は速度改善用で、画像入力の acceptance には不要なため今回の manifest には入れない。

## Fallback Candidate

現行 HauhauCS モデルの `mmproj` が実機で読み込めない場合は、第一候補を `ggml-org/gemma-4-26B-A4B-it-GGUF` に切り替える。対応ファイルは `gemma-4-26B-A4B-it-Q4_K_M.gguf` 約 16,796,015,136 bytes と `mmproj-gemma-4-26B-A4B-it-bf16.gguf` 約 1,194,827,840 bytes。

GPU memory または起動時間が厳しい場合の smoke test 候補は、`ggml-org/gemma-4-E4B-it-GGUF` (`gemma-4-E4B-it-Q4_K_M.gguf` 約 5,335,289,824 bytes + `mmproj-gemma-4-E4B-it-bf16.gguf` 約 991,551,968 bytes)、さらに軽量な `ggml-org/gemma-3-4b-it-GGUF` (`gemma-3-4b-it-Q4_K_M.gguf` 約 2,489,757,856 bytes + `mmproj-model-f16.gguf` 約 851,251,104 bytes)。

## Validation

This run:

- `git diff --check` succeeded.
- `kubectl kustomize argoproj/local-llm >/tmp/lolice-local-llm.yaml` succeeded.
- `kubectl kustomize argoproj/codex-workspace >/tmp/lolice-codex-workspace.yaml` succeeded.
- Rendered `codex-workspace-pi-config` `models.json` was extracted and parsed; `gemma4-26b.input` is `["text", "image"]`.
- `codex review --commit HEAD` initially flagged that `models.ini` is mounted via `subPath`; the Deployment now has `configmap.reloader.stakater.com/reload: "llama-server-models"` so Reloader rolls the Pod when the ConfigMap changes.
- `ghcr.io/boxp/arch/llama-sycl:latest` was pulled at digest `sha256:a592ef922d0b11f9aafdc7c8c1f59449a2fdb90a4a40e945760f7806d996f62e`.
- Running `llama-server --help` in this codex-workspace is blocked by the lack of a SYCL device; it aborts before printing help with `No device of requested type available`.
- As a fallback capability check, copied binaries from the image and confirmed they contain `--mmproj`, `--models-preset`, `--no-mmproj`, `image_url`, `mtmd`, and `mtmd_support_vision` strings.

Local render:

```bash
kubectl kustomize argoproj/local-llm >/tmp/lolice-local-llm.yaml
kubectl kustomize argoproj/codex-workspace >/tmp/lolice-codex-workspace.yaml
```

Container capability check before rollout on the GPU worker:

```bash
docker run --rm ghcr.io/boxp/arch/llama-sycl:latest llama-server --help \
  | rg -- '--mmproj|models-preset|image_url'
```

Host model placement check on the GPU worker:

```bash
ls -lh /var/lib/local-llm/models/Gemma4-26B-A4B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M.gguf \
  /var/lib/local-llm/models/mmproj-Gemma4-26B-A4B-QAT-Uncensored-HauhauCS-Balanced-BF16.gguf
```

OpenAI-compatible smoke test from inside the cluster:

```bash
IMAGE_DATA_URL="data:image/png;base64,$(base64 -w0 ./small-test.png)"

curl -sS http://llama-server.local-llm.svc.cluster.local:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer local-llm' \
  -d @- <<EOF
{
  "model": "gemma4-26b",
  "messages": [
    {
      "role": "user",
      "content": [
        {"type": "text", "text": "この画像に写っている主要な物体、読める文字、目立つ色を日本語で簡潔に説明してください。"},
        {"type": "image_url", "image_url": {"url": "${IMAGE_DATA_URL}"}}
      ]
    }
  ],
  "max_tokens": 256,
  "temperature": 0.2
}
EOF
```

成功条件は、HTTP 200 で返り、回答が画像内の主要物体・文字・色に即していること。失敗時は `llama-server` log、Envoy access log、GPU memory、`/props?model=gemma4-26b`、pi agent が `gemma4-26b` を画像対応候補として選ぶかを確認する。

## Rollback

- `argoproj/local-llm/models-config.yaml` の `mmproj = ...` 行を削除する。
- `argoproj/codex-workspace/configmap.yaml` の `gemma4-26b.input` を `["text"]` に戻す。
- 追加した `mmproj` ファイルはホスト上に残置してよい。ディスク容量を戻す必要がある場合のみ `/var/lib/local-llm/models/mmproj-Gemma4-26B-A4B-QAT-Uncensored-HauhauCS-Balanced-BF16.gguf` を削除する。

## 2026-07-04 Rollback

After the vision rollout, `gemma4-26b` became unavailable from pi agent. Direct health and model list checks against `llama-server.local-llm.svc.cluster.local:8080` still worked, and `ornith-35b` returned `OK` through the same router, but `gemma4-26b` failed with:

```text
500: {"code":500,"message":"model name=gemma4-26b failed to load","type":"server_error"}
```

`/v1/models` reported `gemma4-26b` as `failed: true` with `exit_code: 1` after the `mmproj` preset was added. Roll back only the vision-specific parts for service recovery:

- remove `mmproj = /models/mmproj-Gemma4-26B-A4B-QAT-Uncensored-HauhauCS-Balanced-BF16.gguf`
- change pi agent `gemma4-26b.input` back from `["text", "image"]` to `["text"]`

Keep the projector file on the host if it exists. Reintroduce vision under a separate model ID, for example `gemma4-26b-vision`, only after validating model/projector compatibility and load behavior on `golyat-4`.
