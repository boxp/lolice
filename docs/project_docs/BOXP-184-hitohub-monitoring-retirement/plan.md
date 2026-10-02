# BOXP-184: hitohub監視廃止 — Alertmanager null route 実装

## 目的

サービス終了済み hitohub (stage-hitohub / prod-hitohub namespace) から kube-state-metrics
が生成する汎用アラート（KubePodCrashLooping、KubeStatefulSet* 等）をメール通知の対象外にする。

PR #776 (BOXP-181) により scrape target と stable-diffusion rules は削除済み。
本 PR は Alertmanager 側の route 除外を追加する。

## 変更内容

### `argoproj/prometheus-operator/external-secret-alertmanager.yaml`

`route.routes` に以下の route を追加（severity=critical/warning より先に評価されるよう配置）:

```yaml
- matchers:
    - 'namespace =~ "stage-hitohub|prod-hitohub"'
  receiver: 'null'
```

**配置の根拠**: Alertmanager は最初にマッチした route を使用する（`continue: true` なし）。
hitohub route を critical/warning より前に置くことで、
KubePodCrashLooping (warning) などが Warning receiver（メール）に到達しない。

**他 namespace への影響なし**: matcher が `namespace` ラベルに限定しているため、
control-plane、etcd、longhorn、local-llm 等の warning/critical は従来通り通知される。

## 確認手順（マージ後）

1. `kubectl kustomize argoproj/prometheus-operator` — render 成功を確認
2. Argo CD で prometheus-operator が Synced/Healthy になること
3. ExternalSecret `alertmanager-config` が reload されること（`kubectl -n monitoring get secret alertmanager-main-config` のリビジョンが更新）
4. Alertmanager UI の `/config` で `namespace =~ "stage-hitohub|prod-hitohub"` route が存在すること
5. Prometheus active alerts から stage-hitohub / prod-hitohub 由来のアラートが消えること（または null に吸収されること）

## ロールバック

`git revert` でこの PR を戻すと route が除去され、hitohub 由来アラートが再び Warning receiver に到達する。
