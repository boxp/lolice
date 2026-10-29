# BOXP-364: 生成 ConfigMap 参照不整合の回帰試験

## 背景

- 事象: `codex-workspace` Pod が `MountVolume.SetUp failed for volume "ai-ops-metrics-app": configmap "codex-workspace-ai-ops-metrics" not found` で起動できなかった。
- 原因 (63329224 時点): `argoproj/codex-workspace/kustomization.yaml` に `namespace:` が無く、`configMapGenerator` が生成した
  `codex-workspace-ai-ops-metrics-<hash>` (namespace 未指定) と、`namespace: codex-workspace` を持つ Deployment の
  volume 参照が kustomize の name reference で結び付かなかった。結果、Deployment は hash 無しの名前を参照したまま render された。
- 修正: 7a0d44d1 `Add namespace to kustomization.yaml` (kustomization に `namespace: codex-workspace`)。
  本 PR は fix を重複させず、その fix を保護する回帰試験だけを追加する。関連: BOXP-360 (ai-ops-metrics 追加)。

## 方針

### ① 静的 gate (static-policy job、kind 不要)

- `policy/references/configmap_refs.rego` を追加し、`scripts/static-manifest-check.sh` で render 済みファイルごとに
  `conftest test --combine` 相当で評価する (同一 Application の render 内で参照を解決)。
- 対象参照: `volumes[].configMap`、`volumes[].projected.sources[].configMap`、`envFrom[].configMapRef`、
  `env[].valueFrom.configMapKeyRef` (containers / initContainers、Deployment/StatefulSet/DaemonSet/Job/CronJob/Pod)。
- 規則:
  - `optional: true` の参照は対象外。
  - 参照名が同 render 内・同 namespace の ConfigMap に解決しなければ deny。ただし明示 allowlist
    (cluster が全 namespace に自動作成する `kube-root-ca.crt`) は除外。現時点の全 render で未解決参照は
    `kube-root-ca.crt` のみであることを確認済み。
  - `<参照名>-<10 文字 hash>` の ConfigMap が render 内にあるのに参照が hash 無しのまま → 「generator の name reference
    が解決されていない (namespace 不一致の疑い)」として明示的に deny (今回の障害パターン)。hash 値は固定しない。
  - `configMapKeyRef.key` / `items[].key` が解決先 ConfigMap の data/binaryData に無ければ deny。
  - 契約 (data): `codex-workspace` Deployment の `ai-ops-metrics-app` volume の解決先に `server.py` が必須。
- negative regression: Rego unit test (`conftest verify`) で修正前相当 (namespace 欠落) fixture、存在しない CM、
  別 namespace、必須 key 欠落が deny、修正後相当が pass。加えて修正前 revision の kustomization を実際に render
  して deny されることを手元で確認する。

### ② Chainsaw (kind) E2E: `tests/chainsaw/codex-workspace`

- 本番 `kustomize build argoproj/codex-workspace/` の出力から yq で以下だけを抽出した最小構成を適用する:
  Namespace、生成 ConfigMap `codex-workspace-ai-ops-metrics-*`、Deployment の `ai-ops-metrics` container
  (image / command / probe / securityContext は本番のまま) と `ai-ops-metrics-app` volume (render 後の参照名のまま)。
- `home` volume は emptyDir に置換し、initContainer で stub ledger (`metrics.prom`) を書く。
  workspace 本体 / DIND / SSH / ExternalSecret / 本番 PVC / Gemini 等は持ち込まない。server.py は本番 ConfigMap そのもの。
- assert: Deployment Available (本番 readinessProbe `/healthz`)、Pod 内から `/healthz` = ok、`/metrics` に stub と
  `ai_ops_metrics_file_present 1`。条件 wait + 明示 timeout、sleep 依存なし。
- detect: `argoproj/codex-workspace/` のうち `.argocd-source-*.yaml` / `*.md` のみの変更 (image updater 等) では
  kind を起動しない。workflow 変更時の full set (required_components) にも含める。

## CI 所要

- static gate の追加時間と、codex-workspace chainsaw の cold 所要を PR の CI で実測して owner-summary に記録する。

## スコープ外

- 本番設定変更 / 停止 / 再起動 / PVC 操作 / 新権限。live の復旧確認は read-only 観測のみ。
