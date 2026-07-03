# BOXP-36 Loki PVC サイズ調整

## 目的

Loki の永続ストレージ PVC `monitoring/storage-loki-0` が直近 7 日で 50Gi 上限近くまで到達したため、現在状況と原因候補を確認し、短期再発防止として PVC request を 50Gi から 100Gi に増量する。

## 対象

- Repository: `boxp/lolice`
- Argo CD Application: `argoproj/loki/application.yaml`
- Helm values: `argoproj/loki/helm/values.yaml`
- Namespace: `monitoring`
- StatefulSet Pod: `loki-0`
- Gateway Pod: `loki-gateway-*`
- PVC: `storage-loki-0`
- StorageClass: `longhorn`

## 2026-07-03 18:22 UTC 実測

Grafana API 経由で Prometheus datasource `P1809F7CD0C75ACF3` を確認した。

| 項目 | 値 |
| --- | --- |
| PVC request | 50Gi |
| PVC capacity | 50Gi |
| kubelet 実容量 | 52,521,566,208 bytes / 約 48.91Gi |
| 使用量 | 1,955,401,728 bytes / 約 1.82Gi |
| 空き容量 | 50,549,387,264 bytes / 約 47.08Gi |
| 使用率 | 約 3.72% |
| 1 日増分 | 398,523,528 bytes / 約 0.37Gi |
| 7 日増分 | -50,551,894,798 bytes / 約 -47.08Gi |
| 7 日最大値 | 52,504,788,992 bytes / 約 48.90Gi |
| 7 日最小値 | 24,576 bytes |
| 7 日最大時刻 | 2026-06-28 23:37:14 UTC |
| 7 日最小時刻 | 2026-06-29 04:07:14 UTC |

## Kubernetes live 確認

- `StorageClass/longhorn`
  - `allowVolumeExpansion: true`
  - `provisioner: driver.longhorn.io`
  - `volumeBindingMode: Immediate`
  - `reclaimPolicy: Delete`
  - `parameters.fsType: ext4`
- `PVC monitoring/storage-loki-0`
  - `spec.resources.requests.storage: 50Gi`
  - `status.capacity.storage: 50Gi`
  - `status.phase: Bound`
  - `volumeName: pvc-200a85ff-bb15-4a0d-8fd1-867fefbfe47c`
  - `creationTimestamp: 2026-06-29T04:05:24Z`
- `PV pvc-200a85ff-bb15-4a0d-8fd1-867fefbfe47c`
  - `capacity.storage: 50Gi`
  - `storageClassName: longhorn`
  - `creationTimestamp: 2026-06-29T04:05:35Z`
- Pod 状態
  - `loki-0`: `1/1 Running`
  - `loki-gateway-6876669d67-7n4hz`: `1/1 Running`
  - Prometheus `kube_pod_status_phase` / `kube_pod_container_status_ready` でも両 Pod/container は `1`
- Argo CD Application `loki`
  - `sync.status: OutOfSync`
  - `health.status: Healthy`
  - `operationState.phase: Succeeded`
  - `operationState.finishedAt: 2026-07-03T18:17:11Z`

## 判断

現在使用量だけを見ると増量は不要に見えるが、7 日最大値が約 48.90Gi で 50Gi 上限にほぼ到達していた。`longhorn` は拡張可能で、PVC 縮小は通常できないため、過剰に大きくしすぎない短期対策として 100Gi へ増量する。

今回の 7 日最大値は、少なくとも 2026-06-26 18:22 UTC から 2026-06-28 23:37 UTC 頃まで満杯近くが継続していた。2026-06-29 04:07 UTC 頃に使用量が 24KiB まで低下し、PVC/PV の creationTimestamp が 2026-06-29 04:05 UTC 台であるため、原因は retention / compaction だけではなく、PVC 再作成または StatefulSet 周辺の再作成に近い。

## 変更

- `argoproj/loki/helm/values.yaml`
  - `singleBinary.persistence.size: 50Gi -> 100Gi`

## 適用時の注意

この実行環境の Kubernetes 権限では `persistentvolumeclaims` の `patch` が拒否されたため、ライブ PVC のオンライン拡張は未実施。

既存 StatefulSet の `volumeClaimTemplates` は Kubernetes 側で更新制限があるため、Argo CD / Helm の sync 時に StatefulSet 更新が拒否される可能性がある。その場合は、宣言値を 100Gi に保ったうえで、運用者権限で既存 PVC を先に拡張する。

```bash
kubectl patch pvc -n monitoring storage-loki-0 \
  -p '{"spec":{"resources":{"requests":{"storage":"100Gi"}}}}'
```

適用後は以下を確認する。

```bash
kubectl get pvc -n monitoring storage-loki-0
kubectl get pod -n monitoring -l app.kubernetes.io/name=loki -o wide
```

## 追加チケット候補

- Loki の retention / compaction が期待通りに動いているか確認する
- 満杯前後のログ流入量を Loki datasource で調査し、特定 namespace / pod のログ削減を検討する
- Loki を filesystem 単一 PVC から object storage へ移行する
- PVC / Loki 使用率の alert 閾値と通知経路を確認する
