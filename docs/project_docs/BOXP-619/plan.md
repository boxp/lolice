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

## L1 後の golyat-1 instance-manager 起動阻害と一時措置（CD-04）

- L1 反映後、golyat-1 の v1.10.2 instance-manager が kubelet `OutOfcpu` で error（ノードの CPU requests 3970m/4000m、旧 v1.9.1 IM が 480m を保持）。
- Owner 承認（CD-04）により、shanghai-3 の admin kubeconfig 経路で Longhorn Node `golyat-1` の `spec.instanceManagerCPURequest` だけを 0 → 20（m）へ一時変更（2026-11-19 05:30:10 UTC、JSON Patch の test 付き）。変更前値は 0（global `guaranteed-instance-manager-cpu` v1=12% = 480m を使う）。
- 05:31 UTC に新 IM が running。全 15 volume attached/healthy を確認。
- 復元条件: L1e で全 volume の engine が v1.10.2 になり、golyat-1 の旧 IM（instance-manager-1fdd35a2…）が消えた後、同じ経路で 0 へ戻し、live 値と IM/volume の状態を確認する。

## L1e の方法

- `argoproj/longhorn/settings.yaml` に `concurrent-automatic-engine-upgrade-per-node-limit: "1"` を追加し、Longhorn の自動 engine upgrade で 1 ノードあたり 1 volume ずつ v1.10.2 へ live upgrade する（Pod の再起動・移動は伴わない）。
- 検証: 全 volume の `status.currentImage` が `longhornio/longhorn-engine:v1.10.2`、robustness=healthy、旧 engine image（v1.6.1/v1.9.1）の refCount 0、旧 IM の消滅。

## L1e の結果（2026-11-19 05:40 UTC）

- 全 15 volume の `status.currentImage` が v1.10.2、attached/healthy。engine image v1.6.1/v1.9.1 の refCount 0。backup target available。
- 旧 v1.9.1 instance-manager 4 台は engine プロセスを保持したまま running（replica はすべて新 IM へ移行済み）。v1 data engine の live upgrade では engine プロセスは volume の detach/reattach まで旧 IM に残るため、旧 IM の消滅は各 Pod の再起動待ち。golyat-1 の `instanceManagerCPURequest` の 0 への復元（CD-04）は golyat-1 の旧 IM 消滅後に行う。

## C1: Calico 3.30.2 → 3.32.4 の変更内容

- `argoproj/calico/application.yaml`
  - chart repo を公式 URL `https://docs.tigera.io/calico/charts` へ移し、tigera-operator を `v3.32.4` に更新。
  - 同じ版の `crd.projectcalico.org.v1` chart（CRD 32 件。3.30.2 の 32 件から ANP/BANP 2 件が外れ、clusternetworkpolicies/istios が加わる）を source に追加（CD-02）。
  - `automated.prune` を一時的に false（CD-02）。ANP/BANP の CRD は Argo 上 OutOfSync のまま残る。扱いを決めてから prune を戻す（別 PR）。
- `argoproj/calico/values.yaml`: 3.32 の chart 既定値 `installation.kubeletVolumePluginPath: "None"` は Calico CSI driver を無効にするため、現行と同じ `/var/lib/kubelet` を明示。
- `helm template` で 3.30.2 と 3.32.4 の Installation を比べた差分は、明示値 `/var/lib/kubelet` の維持と、operator 既定値と同じ `controlPlaneReplicas: 2` / `nonPrivileged: Disabled` / 空の nodeSelector・tolerations の明示だけ。

## C1 のマージ前提（Typha の保留）

- 公式 upgrade notes（Hold Typha when upgrading from 3.31）: 3.32 から Typha は 3.32 以降の calico-node だけを受け付ける。既定では operator が Typha を先に更新するため、全 calico-node が同時に接続を失って not ready になり、まとめて入れ替わる。BGP 利用時は各ノードの route が再起動中に withdraw される。
- 本クラスタは Installation に `bgp` の指定がない（既定 Enabled）ため、保留なしで進めると全ノード同時の Pod 間通信断になりうる。
- 公式手順は、更新前に `kubectl annotate deployment calico-typha -n calico-system unsupported.operator.tigera.io/ignore=true` で Typha を保留し、calico-node の rollout 完了後に annotation を外す。codex-workspace の権限ではこの操作ができず、CD-04 の承認範囲（Longhorn Node golyat-1 の CPU 予約のみ）にも含まれないため、Owner の判断を得るまで C1 はマージしない。

## C1 の Chainsaw テスト（TD-01 / CD-06）

- Owner 判断 A（2026-11-19 06:02 UTC）: 既存 shanghai-3 管理者経路で Typha 保留 annotation を一時付与し、C1 merge → calico-node 7/7 rollout・疎通確認 → annotation 除去 → Typha 更新・正常復帰まで。ただし対応する Chainsaw テストを先に用意し、成功を確認してから本番対応する。
- 既存の Chainsaw suite には Calico 更新を扱うものがなかった（共有 kind は CI 冒頭で Calico を fresh install するだけ）。
- 追加: `tests/chainsaw/calico-upgrade/`（`upgrade.sh` / `probes.yaml` / `kind-config.yaml` / `chainsaw-test.yaml`）と `.github/workflows/chainsaw.yaml` の `calico-upgrade` job（`argoproj/calico/` 等の変更時のみ、専用 kind で実行、`status-check` に集約）。
  - prune-safety: base（main）→ head（PR）で Argo desired state から外れる CRD があるなら `automated.prune=false`、Calico が使う CRD は head に残ること、operator 版が chart の組む版と一致すること。
  - install-base: base の application.yaml / values.yaml を Argo と同じ `helm template --include-crds` + SSA で入れる（kind 差分はノードアドレス検出方式のみ）。
  - hold-typha-and-upgrade-calico-node: Typha に `unsupported.operator.tigera.io/ignore=true` を付けて head を適用。calico-node が head 版に揃うまで Typha の image・generation・Pod UID が不変で、calico-node の同時 not-ready が 1 台以下。
  - release-typha-hold: annotation を外すと Typha が head 版になり、全 tigerastatus が Available（Degraded なし）。
  - post-upgrade-checks: 更新中に全ノードから 1 秒ごとに送ったノード跨ぎ HTTP / DNS の失敗率（5% 以下）、更新後の疎通・DNS・NetworkPolicy の deny / allow、Calico CSI driver。
- テスト作成で見つけた PR#1300 の欠陥: `values.yaml` の `tigeraOperator.version: v1.38.4` 固定のままでは chart だけ 3.32.4 になり、Calico 本体（calico-node / typha）は 3.30.x のまま。chart v3.32.4 が組む `v1.42.8` に修正し、prune-safety で検査する。

## C1 の本番手順（テスト成功後）

1. 事前: calico-node 7/7・typha・tigerastatus・Argo calico Synced/Healthy、Longhorn 全 volume healthy を記録。
2. shanghai-3 管理者経路で `kubectl annotate deployment calico-typha -n calico-system unsupported.operator.tigera.io/ignore=true`。
3. PR#1300 を gate 通過済み head で merge → Argo 自動 sync。
4. calico-node 7/7 が v3.32.4 で Ready、Typha が v3.30.2 のまま、DNS / NetworkPolicy / Longhorn volume を確認。
5. annotation を除去 → Typha v3.32.4 rollout、tigerastatus Available、calico-node 7/7 維持を確認。

### ローカル実行結果（隔離 kind、Kubernetes v1.36.4、3 ノード、IPIP Always + BGP Enabled）

- 保留あり（本 suite）: 合格。calico-node 3/3 が v3.32.4 に揃うまで Typha は v3.30.2・Pod 不変、calico-node の同時 not-ready 最大 1、保留解除後 Typha v3.32.4・同時 not-ready 0、更新中の連続疎通 1617 件中失敗 0、更新後の疎通・DNS・NetworkPolicy deny/allow・CSI driver OK。
- 対照（保留なしで同じ head を適用）: operator が Typha を先に v3.32.4 にし（calico-node 更新 0 台の時点）、calico-node の同時 not-ready が 2/3 台に達した。本 suite の判定（Typha 不変・同時 not-ready ≦ 1）はこれを失敗として検出する。kind では dataplane が残り疎通失敗は 0 件だったため、疎通率だけでは保留の有無を判別できない。

## C1 反映時の事象と修正（2026-11-19 07:08〜）

- PR#1300 merge 後、app-of-apps が calico Application の spec を更新する前に calico app が自動 sync し、旧 spec（chart 3.30.2）と新 values（operator v1.42.8）の組み合わせで適用された。tigera-operator v1.42.8 は 3.30.2 の ClusterRole（istios / tier.networkpolicies の権限なし）で cache sync timeout になり再起動を繰り返した。dataplane（calico-node / typha v3.30.2）は影響なし。
- spec 更新後は Argo CD v3.6.2（renovate PR#1306 で同時刻に更新）の server-side diff が CRD chart の whiskers.operator.tigera.io を dry-run する段階で `spec.versions: null` となり ComparisonError、自動 sync が止まった。管理者 kubectl の素の SSA dry-run では同 CRD は正常。
- 修正: calico app に `compare-options: ServerSideDiff=false`（BOXP-501 / BOXP-533 と同じ対処）を付け、CRD の ignoreDifferences から `managedFieldsManagers: [argocd-controller]`（Argo 自身の適用フィールドを全て無視対象にし、RespectIgnoreDifferences で desired から spec.versions が落ちる原因と推定）を外す。preserveUnknownFields の無視は維持。
- Chainsaw suite は kubectl SSA で適用するため、Argo の app-of-apps 反映順序や diff 方式の差はテスト範囲外。

## C1 完了と prune 復元（2026-11-19 07:30〜07:55）

- PR#1308 merge 後に calico app が v3.32.4 で同期（Synced / Healthy）。calico-node は 1 台ずつ v3.32.4 へ更新（同時 not-ready 最大 1）、その間 Typha は保留で v3.30.2 のまま。
- 本番で一時 namespace を使い、ノード跨ぎの Service DNS 経由疎通・NetworkPolicy deny/allow を確認後、Typha 保留 annotation を除去。Typha 3/3 が v3.32.4 で Ready、tigerastatus 全 Available、再度 NetworkPolicy deny/allow OK。
- prune 復元の判断: calico app の `requiresPruning` は 0 件。adminnetworkpolicies / baselineadminnetworkpolicies CRD は Argo の追跡外（tracking-id なし）で、実リソースも 0 件のため、prune=true に戻しても削除されない。CRD は残置（削除は本チケット範囲外）。

## C2: Calico 3.32.4 → 3.33.1（2026-11-19 08:10 UTC〜）

- 前段: C1（3.32.4）と prune 復元（PR#1311、08:03 UTC 反映）の観測完了。calico app Synced/Healthy、CRD 32 件残存、tigerastatus 全 Available、calico-node/csi 7/7、Typha 3/3、Longhorn 15 volume healthy。
- 公式根拠（3.33 upgrade notes / requirements）: 3.33 は 3.31・3.32 から更新可能、Kubernetes 1.35/1.36/1.37 で検証済み。Typha 保留は「3.31 から上げる時」だけで、3.32 → 3.33 には保留手順がない。
- 変更: `application.yaml` の tigera-operator / crd.projectcalico.org.v1 を v3.33.1、`values.yaml` の operator を chart の組む `v1.44.1`、calicoctl tag を v3.33.1。
  - helm template 比較: CRD は 32 件で名前の増減なし（スキーマ更新のみ）、Installation は差分なし（`kubeletVolumePluginPath: /var/lib/kubelet` 維持）。operator の RBAC 追加（replicasets/controllerrevisions 参照、v3 CRD 用 ValidatingAdmissionPolicy、gateways 参照）と image 版のみ。
  - CRD の desired state から外れるものはないため `automated.prune` は true のまま（CD-02 の対象外）。
- 3.33 の要対応項目と本番の該当:
  - 単一 image `calico/calico` への統合: 既定 registry から取得、ミラー・digest 固定なし。
  - policy サイズ上限（rules 1024 / selector 1024 文字など）: 本番 NetworkPolicy(crd) 27 件で最大 rules 13・selector 44 文字、Global/Staged 0 件。
  - FIPS: 未設定。Ingress Gateway / Gateway API: 未使用。eBPF: 無効（Iptables）。
  - IPIP の cluster route を BIRD ではなく Felix が既定で書く: 本番は IPIP Always + BGP。Chainsaw の kind も同じ構成で、ローリング中の混在を疎通で検査する。
  - native v3 CRD への移行は既存クラスタでは自動で切り替わらない（opt-in、本チケットでは行わない）。
- Chainsaw: `tests/chainsaw/calico-upgrade` を、base < 3.32 <= head の時だけ Typha を保留し、それ以外は保留せず operator 既定順序（Typha 先行）で更新する形に拡張。保留なしでも calico-node 同時 not-ready ≦ 1、更新中の連続疎通失敗率 ≦ 5%、更新後の DNS・NetworkPolicy deny/allow・CSI を検査する。C1 相当（3.30→3.32）は従来どおり保留経路。
- 本番手順: Typha 保留は行わない（CD-06 の承認は C1 限定）。gate 通過後 merge → Argo 自動 sync → calico-node 7/7・Typha 3/3 が v3.33.1、tigerastatus 全 Available、ノード跨ぎ疎通・DNS・NetworkPolicy deny/allow、Longhorn volume healthy を確認。
