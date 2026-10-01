# BOXP-206 Tailscale Kubernetes Operator更新 計画（boxp/lolice分）

## 目的と範囲

Tailscale Kubernetes Operatorのchartを1.80.3から1.102.4へ更新する。認証はOAuth clientのまま変えない。

BOXP-206全体の目的は、OperatorをOAuth client secretからWorkload Identity Federation（WIF）へ移行できるかを判断することである。WIFはOperator v1.92以降が必要で、v1.98.3には正しいJWTが拒否される不具合がある（v1.98.4で修正済み）。このため更新はWIFの前提になるが、WIFへ進まない場合でも単独で実施できる。

公開issuerの方式、kube-apiserverのissuer変更、Trust Credential、認証切替の計画はboxp/archの `docs/project_docs/BOXP-206/plan.md`（[boxp/arch#13095](https://github.com/boxp/arch/pull/13095)）に置く。この文書はboxp/loliceで行う変更だけを扱う。

## 変更内容

| ファイル | 変更 |
| --- | --- |
| `argoproj/tailscale-operator/application.yaml` | chartの `targetRevision` を `1.102.4` にする |
| `argoproj/tailscale-operator/helm/values.yaml` | top-levelの `image.tag` を削除する。operator分のresourceを `operatorConfig.resources` へ移す。`proxyConfig.defaultProxyClass` に `default-proxy` を指定する |
| `argoproj/tailscale-operator/proxyclass.yaml` | proxy Podのresourceを指定する `ProxyClass` `default-proxy` を追加する |
| `argoproj/tailscale-operator/kustomization.yaml` | `proxyclass.yaml` を追加する |
| `renovate.json` | argocd managerをこのApplicationに対して有効にする（automergeは無効） |

### valuesのキーを移す理由

従来のtop-levelの `image.tag`、`resources`、`proxyResources` はchartが読まないキーだった。

- imageは元からchartのappVersionに追従していた。`image.tag` を削除しても挙動は変わらない。
- resource制限は実際には適用されておらず、稼働中のDeployment / StatefulSetの `resources` は空だった。chartが読む形式へ移すため、このPRのmerge後に初めて適用される。値は従来の記載と同じにする（operator: 50m/64Mi〜200m/256Mi、proxy: 50m/64Mi〜100m/128Mi）。
- proxy Podのresourceを指定するvaluesキーはchartに無い。`ProxyClass` を作り、既定のProxyClassとして指定する。

### Renovate

argocd managerは既定でファイルパターンを持たないため、chart versionが追跡されていなかった。このApplicationだけを対象に有効化する。Operatorの更新はproxyの再作成を伴うので、automergeは無効にする。他のApplicationは対象に含めない。

## chart 1.80.3 → 1.102.4 の差分

`helm template` で新旧を比較した結果は次の通り。

- CRD追加: `peerrelays`、`proxygrouppolicies`、`tailnets`
- ClusterRole追加: `nodes` と `endpointslices` の読み取り、`validatingadmissionpolicies` / `validatingadmissionpolicybindings` の作成・更新・削除、新CRD 3種
- Role追加（`tailscale-operator` namespace）: `serviceaccounts/token` のcreate（対象は `operator` のみ）、`roles` / `rolebindings` の `deletecollection`
- Deployment: image `v1.102.4`、`PROXY_IMAGE` `tailscale/tailscale:v1.102.4`、env追加（`OPERATOR_SERVICE_ACCOUNT_NAME`、`OPERATOR_LOGIN_SERVER`、`OPERATOR_INGRESS_CLASS_NAME`）
- OAuth secretのmount（`tailscale-operator-oauth`）は変化なし
- 既存のNetworkPolicyで不足する通信は見当たらない（kube-apiserver、TCP 443、UDP 3478は許可済み）

## 影響

- mergeするとArgoCDの自動syncでOperatorと `ts-argocd-server-*` proxyが再作成される。
- Operatorが管理しているのは `argocd/argocd-server` Serviceのexpose（hostname `lolice-argocd`）の1件。利用者は `argocd-diff` workflowで、Tailscale経路が使えない間はCloudflare経路へfallbackする。

## merge後の確認

- Application `tailscale-operator` がSynced/Healthy
- Operator Podのログに認証・reconcileエラーが無い
- `ts-argocd-server-*` が `tailscale/tailscale:v1.102.4` で再作成されReady
- `ProxyClass` `default-proxy` がReadyで、operatorとproxyのPodに `resources` が入り、OOMKilled・再起動が無い
- tailnet上の `lolice-argocd` が到達可能で、argocd-diffがTailscale経路で成功

## rollback

PRをrevertする。syncが繰り返し失敗する場合は、2026-07-05のincident時と同様に `tailscale-operator` の自動syncを一時停止してからrevertする。その間argocd-diffはCloudflare経路へfallbackする。

resource制限が足りずOOMKilledや再起動が起きる場合は、revertせず `values.yaml` と `proxyclass.yaml` の値を上げる。

## 未確認事項

- 22 minorを一度に飛ばす更新の可否。公式に記載がなく、実機では未検証である。
- `OPERATOR_LOGIN_SERVER` はchart既定で空値のenvとして描画される。ArgoCDで差分が残り続けないかはsync後に確認する。
- operatorとproxyの実使用量。metricsを読む権限が無く確認できていない。
- Connector / ProxyGroup等のCRの有無。調査時の権限では一覧できなかった。

## WIFへ進む場合に追加で行う変更（このPRには含めない）

ownerが案A（公開issuerを用意してWIFへ進む）を選んだ場合、boxp/arch側の手順が終わった後に次を行う。詳細と順序はboxp/archのplan.mdに従う。

- `helm/values.yaml` から `oauthSecretVolume` を外し、`oauth.clientId` と `oauth.audience` をTrust CredentialのTerraform outputの値で設定する。ExternalSecretは残したまま切り替える。
- 数日の安定稼働とOAuth clientのrevoke後に、ExternalSecret `tailscale-operator-oauth` を削除する。
- `docs/project_docs/T-20260301-012/runbook.md` のOAuth前提の記述を直す。

## secretの扱い

token、secret、private endpointをticket・PR・ログへ残さない。
