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

## Chainsawによる事前影響検証（レビュー対応）

既存のCI成功はコアコンポーネントAvailableとApplication CR作成の確認だった。今回、kindの標準CNIを無効化してCalicoを導入し、本PRのpolicyが実際に通信を許可/拒否する環境でテストを追加する。

| 試験 | 期待結果 |
| --- | --- |
| policyなしのbaseline、同ns通常Pod/他ns許可ラベルPod→8081/6379 | DNS解決・TCP接続成功 |
| 復元後、同ns server/controller/notifications/applicationset→8081 | TCP成功 |
| 復元後、同ns server/repo-server/controller→6379 | TCP成功 |
| 復元後、同ns非許可ラベル/他ns許可ラベル→8081/6379 | DNS成功、TCP timeout |
| serverへproxy相当PodからServiceアクセス | TCP成功（upstream全許可） |
| fixture Applicationのmanifest生成・sync、復元後hard refresh | 新しいreconcileでSynced/Healthy |

テスト用PodはhostNetwork=falseで、常時NotReadyにして実サービスのReady endpointへの混入を防ぐ。DNS/exec/ツール失敗をTCP拒否と判定しない。試験でpolicyを一時除去する操作はkubeconfig/API endpointを照合した使い捨てkindのみで行う。本番から秘密情報をコピーしない。

実行結果の正本は[PR #817](https://github.com/boxp/lolice/pull/817)本文のCIリンクと完了ログ、ticket Notes、現行月次reportとする。本番のCloudflare/Tailscale認証経路、image-updater通常周期、本番Calico GlobalNetworkPolicyとの合成は隔離fixtureでは再現せず、反映後のowner検証を維持する。

### CI環境の前提と最終ローカル検証

初回の[追加CI](https://github.com/boxp/lolice/actions/runs/37601614219)はCalico導入・全コアPod起動成功、fixture Synced待機timeoutで失敗し、matrix未実行。診断を追加した[再実行](https://github.com/boxp/lolice/actions/runs/37603474997)ではcluster resourcequotas list Forbidden/ComparisonErrorを確認した。

原因はrepoの`manifests/base`単体にcluster RBACが含まれないこと。使い捨てkindだけ、baseと同じrefの公式`manifests/cluster-rbac`を補完して通常upstream install相当の準備をする。最初にCalico API一覧取得の拒否が見えたが、Calico API除外を外しても標準cluster RBACだけでfixture Synced/Healthyになり、根本原因がCI準備の権限不足と分かった。最終実装ではCalicoのresource.exclusions追加を行わない。本番manifest/RBAC/SA/認証情報は追加・コピーしない。

fixtureは固定revisionの`lightweight/app-a`（ConfigMap 1件）。`data.source=app-a`を確認する。guestbook imageの外部pull失敗を通信障害と混同しないため、追加workload imageに依存しない例を用いる。通常PR CIとupgrade preflightのargocd準備に同版cluster-rbacを補完する。preflightはkindnetでmatrix未実施と明示し、fixture生成/syncを確認する。

2026-10-07、専用ローカルkind Kubernetes1.37 / Calico3.33.0で以下が成功した（本番とは別環境）。

- 全コアPod起動、固定revisionからConfigMap生成、fixture Synced/Healthy。
- policy除去後、同nsの試験Pod6種と他ns spoof Podから8081/6379のDNS/TCP成功。
- 復元後、8081のserver/controller/notifications/applicationset、6379のserver/repo-server/controllerがTCP成功。
- 同ns非許可/他ns spoofの両portがDNS成功後2秒TCP timeoutを2回連続。全probeはhostNetwork=false/NotReady。
- 他nsから8084成功、proxy相当ラベルPodからserver 80/443のTCP成功。
- 復元後hard refresh処理完了・reconciledAt更新・Synced/Healthy・ConfigMap内容を確認。
- Calico API除外を除去し、標準cluster RBACのみでfixture生成/sync/Healthyを再確認。

workflow/guardテスト16件、両bash script構文、YAML parse、diff check成功。独立read-only reviewを実施した。最終CIとreview結果はPR本文を参照する。失敗時診断は隔離Application/statusとcontroller/repo-serverログのみでSecretは取得しない。

本番GlobalNetworkPolicyとの合成、Cloudflare/Tailscale認証、image-updater周期は再現していない。隔離環境の成功でも本番反映後ACを完了扱いにしない。
