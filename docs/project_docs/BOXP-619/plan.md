# BOXP-619: Kubernetes 1.36 のまま Calico・Longhorn を対応版へ段階更新する

## 目的と範囲

- Kubernetes 本体（v1.36.5）と CRI-O（1.36.6）は維持し、CNI（Calico）と CSI（Longhorn）だけを 1.37 対応に近い安定版まで段階更新する。
- 承認範囲: lolice の Argo CD Application（`argoproj/calico`、`argoproj/longhorn`）の chart 版更新、通常 CI/review/merge/Argo 反映、観測と通常復旧。
- 承認外: 本番 Kubernetes/CRI-O 1.37 更新、本番 feature gate 変更、PV 移行やデータ破棄、新しい課金や権限の拡大。
- ネットワークとストレージを同時に変えない。1 PR = 1 段とし、前の段の観測が終わるまで次の段をマージしない。

## 現在値（2026-11-19 live 確認）

| 対象 | 現在値 |
|---|---|
| Kubernetes | v1.36.5（7 台）、CRI-O 1.36.6 |
| Calico | chart tigera-operator 3.30.2、operator v1.38.4、calico-node 7/7、typha 3、Argo `calico` Synced/Healthy（automated, prune=true） |
| Longhorn | chart v1.9.1、`current-longhorn-version` v1.9.1、volume 15 件すべて attached/healthy、Longhorn CRD 22 件の storedVersions はすべて `["v1beta2"]` |
| Longhorn engine | v1.9.1 ×12 volume、v1.6.1 ×3 volume（alertmanager-main-db、grafana-pvc、ark-server-data-claim）。2 つの engine image はどちらも CLI API 10 で互換 |
| 復旧点 | backup target（S3）available。日次 backup は 4 volume（codex-workspace-home、hermes-agent-data、etcd-snapshots、ark-server-data-claim）が 2026-11-19 00:00 UTC に Completed。他 volume は 2〜3 replica の冗長のみ（backup 範囲は今回広げない） |

## 公式根拠

### Longhorn

- 「Starting with v1.5.0, Longhorn only supports upgrades from one minor version to the next.」（https://longhorn.io/docs/1.13.0/deploy/upgrade/）。minor の段飛ばし不可、downgrade 不可。
- 1.10 へは 1.9.x から。事前に CRD storage version を v1beta2 だけにする必要がある（https://longhorn.io/docs/archives/1.10.2/important-notes/）。live で全 CRD が `["v1beta2"]` を確認済み。
- 1.13.0 は Kubernetes >= v1.34 が必要。1.36/1.37 のテスト範囲を明記した Longhorn 版はまだない（v1.12.2 の修正項目に 1.37 の名前が出るだけ）。
- 経路: 1.9.1 → 1.10.2 → 1.11.3 → 1.12.2 →（1.13.0 は downgrade 不可のため 1.12.2 到達後に判断）

### Calico

- v3.32 は Kubernetes 1.34〜1.36、v3.33 は 1.35〜1.37 をテスト対象に明記（https://docs.tigera.io/calico/3.32/getting-started/kubernetes/requirements 、https://docs.tigera.io/calico/latest/getting-started/kubernetes/requirements）。現行 3.30.2 は 1.36 をテスト範囲に含まない。
- v3.32 へは v3.15 以降から更新可。v3.33 へは「the two previous Calico releases」（3.31/3.32）から。
- 経路: 3.30.2 → 3.32.4 → 3.33.1

## Calico の注意点（3.32 で chart 構成が変わる）

1. **CRD が tigera-operator chart から外れた**: 3.30.2/3.31.7 の chart は `crds/` に 32 件の CRD を持ち、Argo `calico` app はそれを 32 件の管理リソースとして追跡している。3.32.x の chart には CRD がない（別 chart `crd.projectcalico.org.v1`）。`prune: true` のまま版だけ上げると Argo が CRD 32 件を prune し、IPPool・IPAM・NetworkPolicy などの CR が消えてクラスタのネットワークが止まる。
   - 対策: 同じ Application に `crd.projectcalico.org.v1` chart（同じ版）を source として追加し、CRD を desired state に残す。あわせて移行の PR では `automated.prune` を一時的に false にして、想定外の prune を起こさない。
   - 3.32 の CRD chart にない CRD: `adminnetworkpolicies` / `baselineadminnetworkpolicies`（policy.networking.k8s.io）。3.32 から ANP は enforce されない。prune を止めた状態で OutOfSync を確認し、扱いを決めてから prune を戻す。
   - 新たに入る CRD: `clusternetworkpolicies.policy.networking.k8s.io`、`istios.operator.tigera.io`。
2. **Typha の互換性**: 「From 3.32, Typha accepts connections only from calico-node 3.32 and later. By default the operator upgrades Typha first」。公式は Typha を一時保留する annotation 手順を示す。codex-workspace の権限ではこの annotation を付けられないため、保留を使うか（Owner/管理者の kubectl 操作）、保留なしで felix の一時的な datastore 切断（既存 dataplane は維持され、更新だけが止まる）を受け入れるかを、3.32 の段の前に決める。
3. chart repo URL は公式に https://docs.tigera.io/calico/charts（現行の projectcalico.docs.tigera.io/charts は同内容の index を返すが、更新時に公式 URL へ移す）。

## 段取り

| 段 | 内容 | 検証 |
|---|---|---|
| L1 | Longhorn v1.9.1 → v1.10.2 | manager/driver/UI/CSI が 1.10.2 で Ready、`current-longhorn-version` v1.10.2、全 volume attached/healthy、engine image の incompatible=false、instance-manager 再生成、Argo Synced/Healthy、backup target available、次回日次 backup Completed |
| L1e | engine を v1.10.2 へ（v1.6.1 の 3 件を含む）。自動 engine upgrade 設定で段階的に行う | 全 volume currentImage が新 engine、healthy |
| C1 | Calico 3.30.2 → 3.32.4（CRD chart 追加、prune 一時停止、Typha の扱い決定後） | calico-node/typha/kube-controllers/csi-node-driver 7/7、tigerastatus Available、DNS 疎通、NetworkPolicy の許可/拒否対照、Argo 状態 |
| L2 | Longhorn → v1.11.3 | L1 と同じ |
| C2 | Calico → 3.33.1（whisker HTTPS 化、IPIP route は Felix 管理へ、CNI 1.0.0 は CRI-O 1.24+ で可） | C1 と同じ |
| L3 | Longhorn → v1.12.2（1.12.1 から内部 NetworkPolicy が既定で有効。Calico 上で V1 iSCSI/replica 通信を確認） | L1 と同じ + PVC 再 attach |
| L4 | Longhorn 1.13.0 は downgrade 不可。1.37 テスト範囲の明記がない限り、1.12.2 到達時点で BOXP-618/191 と調整して判断 | — |

各段の共通事項:

- 1 段ごとに PR → CI/codex review → merge → Argo 自動 sync → 観測。次の段は前の段の観測完了後。
- 失敗時は後続を止める。Longhorn は downgrade 不可なので戻さず、通常復旧（Pod 再起動・replica 再構築待ち）で収束させる。Calico は前の chart 版へ revert PR で戻す（CRD は prune 停止中のため残る）。
- 本番 Kubernetes/CRI-O の変更はしない。

## 証跡の接続

- 各段の結果は BOXP-619 の Notes に記録し、BOXP-191 の「CNI 先行更新」「CSI 先行更新」の完了証拠として参照する。

## Argo への反映経路（自動 sync の根拠）

- `argoproj/longhorn/application.yaml` の `spec.syncPolicy.automated`（prune=false、selfHeal=false）で、Git の chart 版変更は自動 sync される。prune/selfHeal を止めているのは削除と巻き戻しだけで、自動 sync 自体は有効。
- live 確認（2026-11-19）: `kubectl get application -n argocd longhorn` の syncPolicy は `{"automated":{"prune":false,"selfHeal":false},...}`。直近の deploy 履歴 id 640〜642（2026-11-06/11-12/11-13）は `operation.initiatedBy.automated=true` の自動 sync。
- Application 自体は親 app `argocd-apps`（`argoproj/kustomization.yaml` に `longhorn/application.yaml` を含む、automated + selfHeal=true）が管理するので、merge 後に targetRevision の変更が live の Application に入り、その後 longhorn app が自動 sync する。
- 手動 sync は不要。merge 後に Argo の sync 状態と revision `v1.10.2` を確認する。
