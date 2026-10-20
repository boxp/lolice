# BOXP-256: external-secrets Application を Server-Side Apply に切り替える

## 背景（トリアージ結果の要約）

- Argo CD `external-secrets` Application は、ESO chart 0.20.4（PR #827、2026-10-13）以降
  `secretstores` / `clustersecretstores` CRD が 256 KiB を超え、client-side apply が書く
  `kubectl.kubernetes.io/last-applied-configuration` annotation の上限 262144 B を超過して
  `metadata.annotations: Too long` で Sync が失敗し続けている。
- 2.13.0 の CRD JSON 実測: `secretstores` 365,295 B / `clustersecretstores` 365,342 B。
  0.18.2（live の schema）は 259,269 B で上限ぎりぎりで通っていた。
- PR #840 の revert（#851）でも復旧せず、#852（2.13.0 再適用、2026-10-20 18:00 UTC merge）後も
  同一エラーで失敗継続を確認した（op Running / retry #4、OutOfSync は CRD 2 件のみ）。
- ESO 本体は稼働し、ExternalSecret 24/25 Ready で秘密配信への影響は観測されていない。
- upstream docs は「CRD が 256KB 上限に達したため全箇所で server-side apply が必須」と明記。
- 詳細は Obsidian `Projects/external-secrets-sync-incident/` を参照。

## 変更内容

1. `argoproj/external-secrets-operator/application.yaml`
   - `syncPolicy.syncOptions` に `ServerSideApply=true` を追加（calico / descheduler / ARC と同形）。
   - 2.13.0 の CRD には `preserveUnknownFields` が無いため、calico で必要だった
     `ignoreDifferences` / `RespectIgnoreDifferences=true` は追加しない。
   - chart 版は main の 2.13.0 を据え置く。
2. `tests/workflows/test_eso_application_sync_options.py`
   - chart 版が 0.20.4 以上なのに `ServerSideApply=true` が無い場合に失敗する fail-closed ガード。
   - kind への fresh `helm install` では Helm が last-applied annotation を書かないため構造的に
     検知できない経路を、静的テストで補う。chainsaw job の
     `python3 -B -m unittest discover -s tests/workflows` で実行される。

## 期待される挙動

- merge 後、Argo CD が SSA で CRD 2 件を更新し、`external-secrets` が Synced になる。
- 既存 CRD の managedFields には `kubectl-client-side-apply` / `argocd-application-controller`
  が残っているが、Argo CD は SSA を `--force-conflicts` 相当で適用するため衝突は所有権の
  移動として解決される（API リソースの削除や再作成は発生しない）。
- CRD の `storedVersions` は変更しない（v1beta1 は served=false のまま残る。BOXP-223）。

## 検証

- ローカル: `kubectl --dry-run=client apply -f argoproj/external-secrets-operator/application.yaml`
- ローカル: `python3 -B -m unittest discover -s tests/workflows -v`
- merge 後（owner または次回 run で実観測）:
  - `kubectl get application -n argocd external-secrets` が Synced / Healthy
  - `kubectl get crd secretstores.external-secrets.io -o jsonpath='{.metadata.generation}'` が 8 から進む
  - `kubectl get externalsecrets -A` の Ready 件数が 24/25 から減らない

## スコープ外（別チケットで提案）

- chainsaw `detect` job の比較基準を `origin/main` から PR base SHA へ変更（merge 後実行で
  components が空になり SKIPPED になる問題）。
- multi-source Application に対する `argocd app diff --local` の是正。
- 旧版→問題版→修正版の kubectl client-side apply 最小再現テスト、`fake` provider による
  ExternalSecret reconcile 検証。
