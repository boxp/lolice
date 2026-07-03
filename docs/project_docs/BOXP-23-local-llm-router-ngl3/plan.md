# BOXP-23 local-llm router NGL3

## 背景

`boxp/lolice#632` で Gemma4 single-model deployment は `-ngl 3` に固定され、`turbo3` を維持したまま `Return exactly: OK` が通る状態になった。

Phase 6 の次段階では、Ooedo と同じ model alias `gemma4-26b` を使えるようにする必要がある。`codex-workspace` / pi agent 統合でも filename ではなく stable alias を使う。

## 変更方針

- `models.ini` を ConfigMap として追加する。
- `[gemma4-26b]` に hostPath 上の Gemma4 GGUF を登録する。
- `load-on-startup = true` で起動時に Gemma4 をloadする。
- Deployment は `-m` 直指定をやめ、`--models-preset /config/models.ini --models-max 1 --no-models-autoload` を使う。
- `-ngl 3 --cache-type-v turbo3 -fa on --jinja --reasoning off --cpu-moe` は維持する。
- image digest は `argocd-image-updater` 管理のままにし、このPRでは触らない。

## 検証

- `kubectl kustomize argoproj/local-llm`
- `kubectl apply --dry-run=server`
- Argo CD sync 後に `/v1/models` で `gemma4-26b` が `loaded` になることを確認する。
- `/v1/chat/completions` に `model: gemma4-26b` を指定して `OK` が返ることを確認する。
