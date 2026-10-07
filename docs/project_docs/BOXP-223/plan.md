# BOXP-223: ESO 更新・権限境界・egress 制限

## このPRの範囲と停止条件

本runは実装要求。前回groomの「コード変更なし」は前回runの範囲と解釈する。
このPRではRenovateによるmulti-source Applicationのchart検出と、ESO専用の手動merge設定を追加する。
chart更新・admission・NetworkPolicyは下記の前提が未完了のため追加しない。
現行のHigh advisoryは未解消であり、チケット全体の完了を意味しない。

2026-10-07のcodex-workspace SAでは次の `kubectl auth can-i` がすべて `no`：

- `list webhooks.generators.external-secrets.io --all-namespaces`
- `list globalnetworkpolicies.crd.projectcalico.org`
- `patch customresourcedefinitions.apiextensions.k8s.io`
- `create pods -n external-secrets`

管理者が承認した既存管理経路を提供すること。SAの権限拡張や別資格情報の探索は実施しない。
AWSの動的IPに追従するサービス限定egress方式・運用担当のowner決定も必要。
Argo ApplicationはselfHeal有効なので、移行前のchart変更をmainへmergeすると先行適用される危険がある。
Renovate検出を有効にしても、ESOのminor/patchを含む全更新のautomergeを無効にし、移行レビュー後にmergeする。
major更新の検出やPR生成は無効化しない。

Renovate更新PRのChainsawが旧ESO版を固定検証する問題の修正は `ci-followup.patch` に保存した。
workflow scope不足により稼働workflowへの反映は未実施。承認済みpush経路でpatch適用後にCIを確認する。

## 現状の再確認

本runのread-only確認：ExternalSecret 29件、非Readyは既存の
`hermes-agent/external-secret-hermes-agent-openweathermap` のみ。
Ready=False / SecretSyncedError、lastTransitionTime=2026-07-28T07:04:00Z。
ESOのcontroller/cert-controller/webhookは各Available=1、実imageはすべてv0.18.2。
Argo external-secretsはSynced/Healthy。
ExternalSecretとClusterSecretStoreのCRDはv1がserved/storage、v1beta1はserved=falseだが
status.storedVersionsにはv1beta1とv1が残っている。GETのv1表示は保存データ移行の証明ではない。
Secret値を取得・掲載しない。既存openweathermap障害の原因・owner・別対応先または承認済み例外は未確定。
関連open PRは確認時点でBOXP-218の#817のみ。ESO更新PRとの重複なし。

## 更新版選定（未完了）

前回調査の第一候補はchart 2.12.0 / appVersion v2.12.0、下限は2.4.1。
本PRは更新版を確定しない。実装再開時点のsupport表・chart index・release notesを再確認する。
0.18.2から選定版までminorごとのbreaking changes、CRD全種のversion/schema、Helm values、
RBAC、AWS provider、image overrideを比較し、必要な中間段階を決める。
Helm renderした3 deploymentの実imageとappVersionを証跡に残す。

修正版確認の対象：

- [GHSA-q7hv-xx6h-q2x8](https://github.com/external-secrets/external-secrets/security/advisories/GHSA-q7hv-xx6h-q2x8)
- [GHSA-r2pg-r6h7-crf3](https://github.com/external-secrets/external-secrets/security/advisories/GHSA-r2pg-r6h7-crf3)
- [GHSA-fcxq-v2r3-cc8h](https://github.com/external-secrets/external-secrets/security/advisories/GHSA-fcxq-v2r3-cc8h)
- CVE-2026-42876/42875およびCVE-2026-22822を含む追加advisory。旧版の対象外判断を継承しない。

参照：[support](https://external-secrets.io/main/introduction/stability-support/)、
[chart index](https://charts.external-secrets.io/index.yaml)、
[2.12.0 release](https://github.com/external-secrets/external-secrets/releases/tag/v2.12.0)。

## 再開後の適用順

1. 承認済み管理経路で全ESO CR/generator参照、Webhook、ClusterGenerator、PushSecret、
   ClusterPushSecret、RoleBinding/ClusterRoleBinding、Calicoの競合policyを棚卸しする。
   利用があればnamespace・主体・用途・参照Secret名・送信先の例外を記録する。
   29件の名前/namespace/refreshInterval/Ready/refreshTimeとStoreの基準を保存する。
2. 全対象CRDのspec.versions/status.storedVersionsを取得する。
   [Kubernetes公式手順](https://kubernetes.io/docs/tasks/extend-kubernetes/custom-resources/custom-resource-definition-versioning/)
   に従い、移行対象の全CRを現storage versionで再保存し、件数・成功・並行更新を確認する。
   その証跡を確認してからstoredVersionsを更新し、旧versionを除くCRDを適用する。
   statusだけの書換え、CRDや生成Secretの削除・再作成を移行方法にしない。
3. 選定版を非本番でrender/schema検証する。AWS template engine v2等の既存manifestを検証する。
   ownerの運用経路でGitOpsの自動同期を制御し、移行→必要な中間版→最終chart更新を段階適用する。
   Application自身を管理する親Applicationの再同期も考慮し、selfHealの先行適用を防ぐ。
4. 未使用確認後、Webhook CREATE/UPDATEとClusterGeneratorのWebhook埋め込みを
   ValidatingAdmissionPolicy+Binding等で拒否する。ClusterGeneratorのkindと埋め込みspec双方を検証する。
   PushSecret/ClusterPushSecretは対象chartの処理無効化values/args、作成権限、admissionを併せて制限する。
   admin/editへ集約されるchart RBACとBindingsを確認し、通常ExternalSecret・必要generatorは維持する。
5. 実SSM/STS endpointとregional/global STS利用、DNS/APIの通信先を確定する。
   AWS private endpointへ至る限定経路、またはSSM/STSだけを許すegress gateway等の実現性をownerと判断する。
   Calico OSS/Kubernetes NetworkPolicyのFQDN非対応を前提とし、単発DNS解決IPの固定や
   任意Internet TCP443、AWS全CIDRの許可を要件充足としない。
   選定経路の障害時動作・更新/監視担当・残余リスクを記録してからpolicyを作る。
   namespace default deny、CoreDNS TCP/UDP53、API Service/backend DNATの実効許可、
   controllerのSSM/STS HTTPS、cert-controller/webhookのAPI通信を別途確認する。
6. Helm render/kustomize build、server-side dry-run、日本語レビューを実施する。
   admissionで拒否すべきCREATE/UPDATEと正常ExternalSecret/必要generatorの許可をserver-sideで試験する。
   同じpolicy selectorを持つ資格情報なし・SA token自動mountなしの検証Podからkubectlで
   DNS/API/許可先成功、許可外IP:443・外部DNS:53の遮断を確認する。
   Calico稼働とGlobalNetworkPolicy等の競合を確認し、試験Podは削除する。Secretによるexfiltration試験は不要。
7. Argo Synced/Healthy、3 deployment Available、parameterstore Readyと29件の名前/ns一致を確認する。
   基準28件はReady=True/SecretSyncedを維持し、それぞれrefreshIntervalの1周期以上の成功を確認する。
   openweathermapは原因と別対応先を記録して29件Readyを目指す。未解消ならowner承認例外が必要。
   新規非Readyは0件。未実施試験を完了扱いにしない。

## 復旧

各段階のrender・CR一覧・安全なバックアップ・Git revisionを管理者側で保存する。
通信制限導入時の障害は直近policyのGitOps差分を戻して通信を復旧し、原因確認後に再適用する。
admissionの不具合は該当Binding/ポリシー差分を戻し、通常CRの処理を確認する。
chartの復旧は移行後のCRD/schemaにcontrollerが対応するかを検証してから行う。
CRDだけのdowngradeを前提にせず、CR/Secret削除を標準復旧にしない。
復旧で脆弱版へ戻る場合はownerの例外・期限・再更新計画を記録する。

## 本PRの検証

Renovate公式[argocd manager](https://docs.renovatebot.com/modules/manager/argocd/)は
managerFilePatternsの明示を要求する。本chartのApplicationだけにpatternを限定する。
実Renovateによるmulti-sourceの抽出・config validation結果はvalidation.mdに記録する。
