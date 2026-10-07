# BOXP-218 検証記録

## 実装前・read-only（2026-10-07 09:10 UTC頃）

- 最新main: `64534cec5e9c64d367346f009fcc96206df594f8`。base/image v3.5.4。
- live `kubectl get netpol -n argocd -o json`: 6件。application-controller/applicationset/dex/notifications、image-updater metrics/webhook。各selectorはrepo-server/redis/serverをselectしない。対象3件は不在。
- `kubectl get applications -n argocd argocd argocd-image-updater -o json`: 両方Synced/Healthy、revision上記main。
- `kubectl get pods -n argocd -o json`: controller/server/repo-server/Redisにupstreamと一致するnameラベル。image-updater v1.3.0、既存restart6回。
- `kubectl get imageupdaters -n argocd -o json`: 12 CR（groom時点の11件から増加）、全件Ready=True/Error=False、lastCheckedAtは09:09:38–09:10:34 UTC。
- `kubectl auth can-i create pods` / `create pods/exec`（argocd）: 両方no。

生JSON・全manifestは一時領域だけで比較し、IP/Secret/tokenを記録へ保存しない。

## ローカル検証

Kustomize v5.8.2、変更前後それぞれ`kustomize build argoproj/argocd`成功。既存のpatchesStrategicMerge deprecation warningあり。

一時Python/PyYAMLで全documentを(apiVersion,kind,namespace,name)のキーで構造比較:

- 変更前53件、変更後56件。
- 差分はNetworkPolicy3件の追加のみ、全既存53件は完全一致。
- 各対象policyはnamespace argocdに各1件。
- 宛先selector、Ingressのみ、8081/6379の正確な同ns peer/portをassert。
- repo-serverの8084 namespaceSelector:{}、serverのingress:[{}]もassert。
- `git diff --check`成功。

## 独立レビュー・CI

結果はPR本文に記載する。codex-review（Git差分）とcodex-review-file（生成物比較）を別プロセスで実施する。既存Chainsaw CIはkind上のDeployment Available等を検証するが、本番CNI遮断の証拠にはならない。argocd-diff CIのAuth pathは一方のみで、両外部経路の正常性の証拠にはしない。

## 未確認・引継ぎ

本番merge/syncは未実施。反映後3policy、通常manifest生成reconcile、許可peer TCP成功、無許可PodのTCP拒否、CNI enforcementと加算的GlobalNetworkPolicy、Cloudflare/Tailscale個別認証アクセス、反映後image-updater継続は未確認。

owner（boxp）または指定担当が既存承認済み経路でplan.mdの手順を実施する。現SA権限を拡張せず、証拠取得まで該当ACを未チェックで残す。現行月次正本にも実装前/未反映を追記する。
