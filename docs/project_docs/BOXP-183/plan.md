# BOXP-183 local-llm llama-server CrashLoop 修復

## 調査結果

- `llama-server` は `golyat-4` に正常にスケジュールされ、Intel i915 GPU も割り当て済みである。
- 現行の `sha256:34765b0917ef...` は起動直後に終了コード `132`（SIGILL）で停止する。
- Argo CD Image Updater が 2026-08-22 に、最後に稼働していた
  `sha256:836a64d77555...` から当該 digest へ自動更新した時刻と、Deployment が
  `Available=False` になった時刻が一致する。
- `pods/log` の取得権限はないが、Pod 状態、イベント、rollout 履歴、ImageUpdater の
  `recentUpdates` で原因を追跡できる。

## 対応

1. `argoproj/local-llm/.argocd-source-local-llm.yaml` に記録済みの稼働 digest
   `sha256:836a64d77555...` をロールバック先として維持する。
2. 検証されていない `latest` digest が同じ経路で再投入されないよう、
   `local-llm` の Argo CD ImageUpdater を削除する。
3. Argo CD 同期後に Pod Ready、Service 経由の `/health` と推論リクエスト、
   CrashLoop および 5xx 通知の解消を確認する。

## 検証

- `kubectl kustomize argoproj/local-llm`
- `kubectl apply --dry-run=server -k argoproj/local-llm`
- Argo CD が `Synced` / `Healthy` となること
- `llama-server` が Ready となり、Service/VIP 経由のヘルスチェックと推論が成功すること
