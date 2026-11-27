# BOXP-724: KubeMemoryOvercommit を lolice の配置制約に合う監視へ整理する

## 背景

BOXP-720 のトリアージで、kube-prometheus 同梱の `KubeMemoryOvercommit`（warning、メール通知）は過去 30 日の約 71%、直近 7 日の 99.6% で発火していた。
上流の式は「全 Pod の memory requests 合計 > allocatable 合計 − 最大ノード」で、

- GPU ノード golyat-4 でしか動けない llama-server（40Gi）など、ノードが落ちたら止まる前提の Pod も退避先が要るものとして数える
- DaemonSet の Pod（ノードと一緒に消える）も数える
- control-plane taint の shanghai-1..3 の空きを退避先に数える

ため、退避の判断に使えなかった。

## 方針

- 上流の `KubeMemoryOvercommit` だけを kustomize の json6902 patch で外す（`patches/remove-kube-memory-overcommit.yaml`）。
  `test` op で位置を確かめるので、上流の更新で並びが変われば build が失敗し、別のルールを黙って消さない。
- 代わりに `node-failover-memory-rules.yaml` の `LoliceNodeFailoverMemoryShortfall`（warning、`for: 15m`、label `node`）を追加する。通知経路は既存の warning と同じ（Alertmanager の route は変えない）。
- `KubeCPUOvercommit` ほか既存のルールは変えない。Pod の配置・requests、朝報、Alertmanager の route は変えない。silence は使わない。

## 判定（仕様: Obsidian `Projects/node-failover-memory-alert/spec.md`）

ノード N ごとに

- 退避可能 requests(N) = N 上の Pending/Running Pod の memory requests。DaemonSet の Pod と GPU 拡張リソース（`gpu.intel.com/i915|xe`、`nvidia.com/gpu`、`amd.com/gpu`）を要求する Pod は除く
- 退避先 = NoSchedule/NoExecute taint・cordon・NotReady でないノード。空き = max(allocatable − requests, 0)
- 不足量(N) = 退避可能 requests(N) − (退避先の空きの合計 − N 自身の空き)

不足量 > 0 が 15 分続いたら発火する。記録ルール `node:lolice_failover_memory_shortfall:bytes` などで余裕を常に見られる。

### 区別（このルールの外、既存のまま）

| 事象 | 検知 |
|---|---|
| golyat-4 / GPU サービスの停止 | `KubeNodeNotReady` / `KubeNodeUnreachable` / `LocalLlm*` / `GpuBackend*` |
| 実メモリの逼迫 | `NodeMemoryHighUtilization` / `NodeMemoryMajorPagesFaults` / `KubeNodePressure` / `KubeNodeEviction` |
| 退避可能 Pod の容量不足 | `LoliceNodeFailoverMemoryShortfall`（今回） |

### 既知の限界

- bin packing（1 つの大きな Pod が 1 ノードに収まるか）は見ない（上流と同じ）。
- hostname の nodeAffinity / nodeSelector で固定された Pod（drama3 2Gi、kohako-stt 1Gi など）と GPU ラベルの nodeSelector だけの小さな Pod は判別できないので退避可能として数える（安全側、現在 約3.4Gi）。
- 退避先の affinity / arch、CPU requests、PDB、2 台同時の喪失は見ない。
- Lean モデルは作らない: 状態遷移や順序の規則がない入出力の式で、promtool の単体テストで仕様の例を直接確かめるため。

## 確認

- `tests/node-failover-memory-rules.test.yaml`: 仕様の例 1〜6（GPU/DaemonSet 除外、不足で発火、taint・cordon・NotReady ノードを空きに数えない、終了済み Pod を数えない、短時間は鳴らない）。期待値は仕様の GiB 表から手計算。ルール追加前に失敗することを確認済み。
- 実データ（2026-11-27 03:3x UTC、read-only）: 同じ式で golyat-4 喪失時の不足量は −20.07GiB（鳴らない）。旧ルールは +10.65GiB で発火中。
- 過去 30 日（1h 刻み）: 新しい式の不足量 > 0 は 0 回、最も迫ったのは golyat-4 の −4.42GiB。
- `kubectl kustomize argoproj/prometheus-operator`: 差分は `KubeMemoryOvercommit` の削除と新ルールの追加だけ。patch の位置を変えると build が失敗する。

## 発火したとき（runbook）

1. `node:lolice_failover_memory_shortfall:bytes` と `node:lolice_relocatable_memory_requests:sum` で、どのノードの喪失で何 GiB 足りないかを見る。
2. 対象ノード上の退避可能 Pod の requests 上位を `namespace_pod_node:lolice_memory_requests:sum{node="<node>"}` で見る。
3. 使っていないワークロード（ゲームサーバなど）の停止、requests の見直し、ノード追加を Owner が判断する。実メモリの逼迫ではないので即時対応は不要。

## 反映後の確認

- Argo CD `prometheus-operator` が Synced/Healthy。
- Prometheus の `/api/v1/rules` に `LoliceNodeFailoverMemoryShortfall` があり、`KubeMemoryOvercommit` がない。
- `ALERTS{alertname="KubeMemoryOvercommit"}` が消え、Alertmanager にも残っていない。
