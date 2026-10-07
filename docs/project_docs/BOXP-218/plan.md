# BOXP-218: Argo CD NetworkPolicy 復元

## 計画

- [x] 指定run worktreeに最新main `64534ce` を取り込み、base/live image v3.5.4を確認する。
- [x] 削除履歴・既存policy・正当な通信を確認する。
- [x] 3削除overlayとkustomization参照を除去し、upstreamを復元する。
- [x] Kustomize生成物の件数・namespace・peer・portと他リソース不変を検証し、独立review・CI結果を記録する。
- [ ] PRを作成し、ownerがレビューして承認済みGitOps経路で反映する。
- [ ] ownerの既存許可済み経路で機能継続・接続許可/拒否・CNI実効を確認する。
- [ ] 現行月次reportへ反映前後の証拠と未確認を追記する。

このPRは復元実装まで。本番反映・live検証の未完了をPR作成で完了扱いにしない。カードlaneはrunnerが管理する。

## 調査・判断

2026-10-07 UTC、main `64534ce`。提供snapshotの2commitはmainへ適用済みでrebase時にskipされ、base v3.5.4を維持した。

`84e6b97`（2024-04-18 JST、message: add: removing network policies）は3削除patch追加のみ。前後のgit履歴、対象patchの履歴（後続`324b0a7`はnamespace除去）、GitHub commitの関連PR/コメントAPI（いずれも空）、BOXP-218 Notesを確認した。削除理由は**不明**。直前Calico導入・当時LoadBalancerは仮説の背景に留める。cloudflared追加は翌日で因果を裏付けない。

liveではNetworkPolicy6件、対象repo-server/redis/serverをselectするpolicyなし。Argo CD image v3.5.4、image-updater v1.3.0。argocdとargocd-image-updaterはSynced/Healthy、main revision `64534ce`。image-updater既存restart6回は反映前基準。CNI GlobalNetworkPolicyとの合成・実到達性は未確認。

## 復元される通信

宛先はnamespace `argocd`、selector `app.kubernetes.io/name=<宛先名>`。以下はv3.5.4 upstreamのまま。Ingress policyでありegressは変更しない。

| 宛先 | TCP port | 許可元 | 用途 |
| --- | --- | --- | --- |
| argocd-repo-server | 8081 | 同nsでnameがargocd-server / argocd-application-controller / argocd-notifications-controller / argocd-applicationset-controller | manifest生成等gRPC |
| argocd-repo-server | 8084 | 全namespaceのPod（namespaceSelector: {}） | metrics、upstream仕様 |
| argocd-redis | 6379 | 同nsでnameがargocd-server / argocd-repo-server / argocd-application-controller | cache |
| argocd-server | 全port | 全ingress（ingress: [{}]） | upstream仕様。Service 80/443→Pod8080、UI/API |

nameは`app.kubernetes.io/name`。同ns peerはpodSelectorだけで表す。8081/6379に全ns/全Pod許可を追加しない。serverは復元しても通信制限にならないためCloudflare/Tailscale例外は不要。image-updaterはApplication CR・registry・Git write-back構成で直接repo-server/Redis依存未確認のため例外を設けない。動的Pod名・UUIDは使用しない。

## 反映と検証（owner担当・未実施）

実施者はowner（boxp）またはownerが指定する既存承認済み検証経路の担当者。現worker SAはpods create/exec不可。新規credential/RBAC付与はしない。

1. PR承認後mainへmergeし、通常のArgo CD automated GitOps syncを待つ。直接kubectl applyは行わない。反映前のApplication状態、通常Podラベル/hostNetwork、image-updater CRのlastCheckedAt/Ready/Errorとrestartを保存する。既存別ApplicationのOutOfSync等を区別する。
2. `kubectl get netpol -n argocd` と3件の`-o yaml`で各1件、selector/peer/portを確認。他の加算的policy・Calico policy・CNI enforcementも既存許可済み経路で確認する。
3. argocd/image-updaterのSynced/Healthyと新規manifest生成を伴う通常reconcileを確認。image-updaterは通常周期でlastCheckedAt更新、Ready=True/Error=Falseを確認（更新対象なしでも成功）。
4. Cloudflare TunnelとTailscaleをそれぞれ指定して通常の認証済みUI/APIアクセスを確認する。argocd-diff CIのfallback成功をTailscaleの成功と扱わない。
5. 許可Podからrepo-server:8081 / redis:6379へのTCP接続成功を確認。同ns非許可ラベルPodと他nsの通常Podから両portへの拒否を対照確認する。DNSとService解決、送信元egress許可、許可peerの成功で単なるDNS/egress障害を排除する。Service経由とPod宛の切り分けも記録する。
6. 試験元ns/labels/hostNetwork=false、宛先port、timeout（例5秒）、時刻、許可/拒否結果を安全な要約で記録。IP/raw log/Secret/tokenを保存しない。RCE/Redis書込みは試験しない。hostNetwork/ノード/管理者を含む全主体遮断は主張しない。
7. `Projects/monthly-security-audit/reports/2026-10-07`へ反映後policy・機能継続・許可/拒否・CNI証拠と未確認を追記する。証拠不足のACは未チェックで残す。

## Rollback

通信障害が新規に発生した場合、ownerの承認済み経路でこの復元commitをGit revertしmainへ反映、GitOps syncを待つ。削除patch復活により境界欠落が再発するため緩和撤回を明記し、再調査する。単発kubectl deleteで迂回しない。revert後のpolicy状態、Application/reconcile、両外部経路、image-updaterを再確認し、月次reportへ結果を記録する。

## 根拠

- [削除commit](https://github.com/boxp/lolice/commit/84e6b97)
- [repo-server upstream](https://github.com/argoproj/argo-cd/blob/v3.5.4/manifests/base/repo-server/argocd-repo-server-network-policy.yaml)
- [Redis upstream](https://github.com/argoproj/argo-cd/blob/v3.5.4/manifests/base/redis/argocd-redis-network-policy.yaml)
- [server upstream](https://github.com/argoproj/argo-cd/blob/v3.5.4/manifests/base/server/argocd-server-network-policy.yaml)

CVE/Helm版数の評価はBOXP-218 Contextと現行月次reportを引き継ぐ。chart修正版10.0.0をArgo CD binary版へ適用しない。

## レビュー指摘への追加計画（2026-10-07）

ownerから「policy復元前にChainsawで影響を検証できないか」と指摘を受け、隔離環境での事前検証を追加する。

- [x] 既存PR #817・CIログ・テストを確認する。従来の成功はコアPod AvailableとApplication CR作成のみで、通常のmanifest生成やTCP通信は未検証。標準kindnetではNetworkPolicy enforcementを検証できない。
- [x] policyを実施するCNI付きの専用kind環境を用意し、本PRのKustomize生成物を適用する。
- [ ] 正当な同namespace peerラベルからrepo-server:8081 / Redis:6379への接続成功と、同namespace非許可ラベル・別namespace（許可ラベルを付けても）の拒否を対照試験する。DNS・policyなしでの到達成功により単なるDNS/egress障害を除外する。
- [ ] controllerによる実際のmanifest生成・reconcileをfixture Applicationで確認する。本番を参照する自己管理Applicationの自動syncでPRのpolicyが上書きされない構成にする。
- [ ] ローカル可能な検証・独立レビューとGitHub Actionsで実行し、PR・validation・現行月次report・Notesへ結果と限界を追記する。

隔離kindの通信試験は本番への能動試験と分けて扱う。本番CNIの加算的policy、Cloudflare/Tailscaleの認証経路、image-updater通常周期は引き続きownerによる反映後検証が必要。fixture peerによるTCP成功は実コンポーネントの全操作成功を保証しないため、manifest生成の機能検証も併用する。

[Calico公式kind導入手順](https://docs.tigera.io/calico/latest/getting-started/kubernetes/kind) と [kind公式CNI設定](https://kind.sigs.k8s.io/docs/user/configuration/) を参照。CNI/クラスタはCI用だけに追加し、本番マニフェストの許可peerは変更しない。

CI初回のfixture失敗はCalico3.33管理APIのtiered RBAC/list拒否によるcache初期化エラーとしてローカルで再現した。CI専用のresource.exclusionsを追加し、本番設定/RBACを変えずに再検証する。最終CI結果はPR本文・Notes・現行月次reportを正本として参照する。

base単体にcluster RBACが含まれないため、使い捨てkindへbaseと同版のupstream cluster-rbacを補完してfixture同期を試す。本番の権限拡張はしない。詳細はvalidation.mdのCI環境差を参照。
