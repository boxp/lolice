# BOXP-23 local-llm NGL3

## 背景

`golyat-4` 上の Gemma4 は `turbo3` と `-fa on` で起動するが、`-ngl 99` では completion content が崩れる。

実機で `llama-cpp-turboquant` `76d7e0727abd24247bcc8b62b9820e8685efbb7c` を host build して切り分けた結果、Kubernetes 外でも GPU offload 時に同じ症状が再現した。`-ngl 0` は `OK` を返し、`-ngl 1` / `2` / `3` も `OK`、`-ngl 4` 以上で崩れた。

## 変更方針

- `turbo3` は維持する。
- `argocd-image-updater` 管理の image digest は手動変更しない。
- `local-llm` の `-ngl` を `3` に下げ、現時点で正常出力が確認できた範囲に固定する。
- Gemma4 の chat template / thinking を明示するため `--jinja --reasoning off` を追加する。
- Ooedo 側の既知設定に合わせて `--cpu-moe` を追加する。

## 検証

- `kubectl kustomize argoproj/local-llm`
- `kubectl apply --dry-run=server`
- Argo CD sync 後に `/health`、`/v1/models`、`/v1/chat/completions` を `golyat-4` から確認する。

## 後続

- `-ngl 4` 以上で崩れる Intel SYCL / Gemma4 / TurboQuant correctness issue は別途追跡する。
- router ConfigMap と `codex-workspace` / pi agent 統合は、`-ngl 3` のGitOps smoke後に進める。
