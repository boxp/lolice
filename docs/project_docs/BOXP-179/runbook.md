# BOXP-179 Control-plane alerts runbook

## Control-plane NodeReady or NodeLease

1. Alertmanager で対象 node と発火時刻を確認する。
2. `kubectl get node <node>` と kubelet の journal を確認し、ネットワーク断・kubelet 停止・ディスク枯渇を切り分ける。
3. etcd member でもある node は、復旧または交換前に quorum が 2 台以上あることを確認する。

## etcd healthy endpoints

1. `kubectl -n kube-system get endpoints etcd` と etcd metrics scrape の `up{job="etcd"}` を確認する。
2. 2 endpoint の warning は冗長性喪失であり、追加障害前に停止 node を復旧する。
3. 2 未満の critical は quorum 危機である。書き込みを伴う操作を避け、健全 member を保持して障害 member を復旧する。

## etcd WAL fsync latency

1. 対象 instance の disk utilization、read latency、filesystem read-only 状態を確認する。
2. SD カードの I/O 劣化が疑われる場合は、quorum を保ったまま node を drain・交換する。

## Control-plane disk I/O latency

1. `node_disk_*` の対象 `mmcblk` device と disk busy を確認する。
2. sustained latency または read-only filesystem は媒体故障として扱い、データ保全後に交換する。
