# BOXP-212 Longhorn storage alerts runbook

対象ルール: `argoproj/prometheus-operator/longhorn-storage-rules.yaml`（group `longhorn-storage.rules`）。
背景と回収手順の詳細は BOXP-211 の Notes（Loki / ark / codex-workspace-home の trim、fallocate、レプリカ再配置 (C)）を参照する。

共通の確認場所:

- Longhorn UI（cloudflared tunnel 経由）: Node 画面で各 disk の Allocated / Max / Conditions、Volume 画面で Actual Size と Snapshots。
- Prometheus / Grafana Explore: `longhorn_disk_status`、`longhorn_disk_usage_bytes / longhorn_disk_capacity_bytes`、`max by (pvc_namespace, pvc) (longhorn_volume_actual_size_bytes)`、`kubelet_volume_stats_used_bytes`。
- kubectl（read-only）: `kubectl -n longhorn-system get nodes.longhorn.io -o json | jq '.items[].status.diskStatus'`、`kubectl -n longhorn-system get volumes.longhorn.io`。

## Longhorn disk not schedulable

`LonghornDiskNotSchedulable`（warning, for 15m）。`longhorn_disk_status{condition="schedulable"} == 0` が 15 分続いた。

1. Longhorn UI > Node > 対象 node で Conditions の reason を見る。典型は予約量（Allocated）が上限（Max = capacity × (1 − reserved) × over-provisioning）を超えたケース（BOXP-211）。`longhorn_disk_reservation_bytes` と `longhorn_disk_usage_bytes` も併せて確認する。
2. Longhorn 1.9.1 は Allocated をメトリクスに出さないため、UI または `nodes.longhorn.io` の `status.diskStatus.*.storageScheduled` で確認する。
3. 予約超過なら、不要ボリュームの削除、または BOXP-211 Notes の承認依頼 (C) 改訂版の手順で対象ボリュームのレプリカを空きのある node（golyat-4）へ再配置する。レプリカ移動は 1 ボリュームずつ行い、`replicaAutoBalance` を一時的に `disabled` にしてから count を増減する。
4. DiskPressure（実使用量）が理由なら「Longhorn disk usage」の手順へ。
5. 復旧後、`longhorn_disk_status{condition="schedulable"}` が 1 に戻り、アラートが resolved になることを確認する。

## Longhorn disk usage

`LonghornDiskUsageHigh`（warning, 80%, for 30m）/ `LonghornDiskUsageCritical`（critical, 90%, for 10m）。`longhorn_disk_usage_bytes` は disk が載っている filesystem 全体の使用量で、golyat-1〜3 では root FS と共有している。node-exporter の `NodeFilesystemAlmostOutOfSpace`（5% / 3%）より先に鳴る位置づけ。

1. Longhorn UI > Volume で Actual Size の大きい順に並べ、FS 使用量（`kubelet_volume_stats_used_bytes`）と乖離しているボリュームを探す。乖離があれば「Longhorn volume actual size diverged」の手順で回収する。
2. Longhorn 以外の使用量（コンテナイメージ、ログ、journal）は `node_filesystem_*` と `df` で切り分ける。BOXP-211 Notes の承認依頼 (D) を参照。
3. 回収できない場合は、レプリカ再配置（上記 (C)）で golyat-4 へ移す。
4. critical の場合は kubelet eviction（root FS の空きが閾値を切ると Pod が追い出される）と Longhorn のレプリカ failure が迫っているため、まず即効性のある trim / snapshot 削除から行う。

## Longhorn volume actual size diverged

`LonghornVolumeActualSizeDiverged`（warning, for 6h）。Longhorn の実サイズが FS 使用量の 3 倍超かつ差分 20GB 超の状態が 6 時間続いた。毎日 02:00 UTC の filesystem-trim（RecurringJob `filesystem-trim`、`remove-snapshots-during-filesystem-trim=true`）で回収されるはずの領域が残っている。

1. Longhorn UI > Volume > 対象ボリューム > Snapshots で、removed 済み snapshot が残っていないか、backup 用 snapshot が肥大していないかを見る。
2. 対象ボリュームに `recurring-job.longhorn.io/filesystem-trim=enabled` ラベルが付いているか確認する（CronJob `filesystem-trim-label-sync` が 01:50 UTC に付与する）。detached のボリュームは trim 対象外。
3. Longhorn UI で Trim Filesystem を手動実行する。ext4 は trim 済みブロックグループを記憶しているため効かないことがある。その場合は Pod 内で空きサイズ分を `fallocate` して即削除してから再度 trim する（BOXP-211 Notes 2026-10-02 07:18 の手順）。Pod を再起動して remount しても解除できる。
4. 回収後、`longhorn_volume_actual_size_bytes` が FS 使用量に近づくことを確認する。backup snapshot（00:00 UTC）直後は一時的に増えるので、翌 02:00 UTC の trim 後に再確認する。

## Longhorn metrics absent

`LonghornMetricsAbsent`（warning, for 30m）。`up{job="longhorn-backend", namespace="longhorn-system"} == 1` が 1 つも無い。他の Longhorn アラートが全て見えなくなっている。

1. Prometheus > Status > Targets で `longhorn-system/longhorn-prometheus-servicemonitor` の target があるか、lastError を見る。
2. target 自体が無い場合は ServiceMonitor の opt-in ラベル `app.kubernetes.io/part-of: kube-prometheus` が外れていないか確認する（`argoproj/longhorn/application.yaml` の `metrics.serviceMonitor.additionalLabels`、PR #811）。Argo CD の Application longhorn が OutOfSync なら sync する。
3. target はあるが DOWN の場合は `kubectl -n longhorn-system get pods -l app=longhorn-manager` と Service `longhorn-backend` の port 名 `manager` を確認する。
4. Prometheus 側の RBAC（ClusterRole `prometheus-servicemonitor-role`）と NetworkPolicy（`overlays/network-policy-prometheus-k8s.yaml`）に変更が無いか確認する。
