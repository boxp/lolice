# BOXP-254: Grafana Alloy がコンテナログを 1 行も Loki に送っていない問題の修復

## 背景

2026-07-05 の INC-1 対応で Alloy を `loki.source.kubernetes`（pods/log API）から
`loki.source.file`（ノードのファイル tail）に切り替えた際、`discovery.relabel` が
`__path__` に `/var/log/containers/<pod>_<ns>_<container>-*.log` という glob を設定した。
`loki.source.file` は glob を展開せず絶対パスしか受け付けないため、
`stat .../*.log: no such file or directory` となって 1 ファイルも tail されず、
2026-07-04 17:20 UTC 頃から Loki に 1 行もログが届いていなかった。
Grafana の Loki パネル（`ark asa` 2 枚、`lolice` node logs、`minecraft` log、
`Local LLM / GPU Overview` Token Throughput）と Explore の LogQL が全て「No data」になっていた。

調査の詳細は Obsidian vault の `Projects/grafana-loki-panel-triage/` を参照。

## 変更内容

1. `argoproj/grafana-alloy/configmap.yaml`
   - `local.file_match "pods" { path_targets = discovery.relabel.pods.output }` を追加し、
     `__path__` の glob を実在ファイルに展開する（ラベルはそのまま引き継がれる）。
   - `loki.source.file "pods"` の `targets` を `local.file_match.pods.targets` に変更。
   - `__path__` の生成ルール自体は変更しない。
2. Alloy の ServiceMonitor を Prometheus に拾わせる
   - 稼働中の Prometheus `monitoring/k8s` の `serviceMonitorSelector` は
     `app.kubernetes.io/part-of=kube-prometheus`（kubectl で確認、Loki の SM も同ラベル）。
   - Helm chart alloy 0.12.0 の values キーは `serviceMonitor.additionalLabels` であり、
     従来の `serviceMonitor.labels`（origin/main では `app.kubernetes.io/part-of: kube-prometheus`）は無視されていた。
     さらに chart は常に `app.kubernetes.io/part-of: alloy` を出力するため、
     `additionalLabels` で同キーを足すと YAML の重複キーになる。
   - そのため chart 側 `serviceMonitor.enabled: false` にし、
     `argoproj/grafana-alloy/servicemonitor.yaml` を kustomize リソースとして追加した
     （Service `monitoring/grafana-alloy` の selector `app.kubernetes.io/name=alloy,
     app.kubernetes.io/instance=grafana-alloy`、port `http-metrics`）。
   - これで `loki_source_file_files_active` / `loki_write_sent_entries_total` 等が
     Prometheus に入り、「Loki に何も届かない」を今後検知できる。

## 検証（ローカル、grafana/alloy:v1.7.0 イメージ）

- `alloy fmt` で新設定が構文エラーなく整形できることを確認（インデント差のみ）。
- `discovery.kubernetes` を静的 target に置き換え、`/var/log/containers/test-pod_monitoring_app-abc123.log`
  （symlink）を用意して `alloy run` したところ、
  - 新設定: `local.file_match.pods` → `loki.source.file.pods` が評価され
    `tail routine: started path=/var/log/containers/test-pod_monitoring_app-abc123.log` を確認。
  - 旧設定: `failed to tail file, stat failed ... test-pod_monitoring_app-*.log: no such file or directory`
    で tail 0 件（本番の症状を再現）。
- `kubectl kustomize argoproj/grafana-alloy` が成功。

## 反映時の挙動・副作用

- Argo CD `grafana-alloy` は automated sync。ConfigMap は config-reloader サイドカーにより
  Pod 再作成なしで `/-/reload` される。Helm values 変更（SM 無効化）は chart の ServiceMonitor を
  prune し、kustomize 側の ServiceMonitor（同名 `grafana-alloy`）を作成する。
  同名リソースの付け替えのため、Argo CD の sync 順序によっては一時的に ServiceMonitor が
  無い瞬間があるが、ログ収集には影響しない。
- 反映直後、各ノードの既存ログファイルを先頭から読むため Loki へのバーストが発生する。
  `reject_old_samples_max_age 168h` を超える行は Loki が拒否し
  `loki_discarded_samples_total` が増えるが想定内。containerd のローテーション上限
  （既定 10MiB × 5）があるので量は有限。
- rollback: 本 PR の revert のみ（Argo CD 自動同期）。

## 反映後の確認（チケット AC）

- Prometheus: `rate(loki_distributor_lines_received_total[5m]) > 0`、
  `loki_request_duration_seconds_count{route="loki_api_v1_push",status_code="204"}` 増加、
  `up{job="monitoring/grafana-alloy"}` が 7 ノード分 1、`loki_source_file_files_active > 0`。
- Grafana Loki datasource: `/loki/api/v1/labels` が `namespace` 等を返し、
  `{namespace="monitoring"}` と `{node="golyat-1"}` 等 7 ノード分が直近ログを返す。
- 上記 4 dashboard / 5 panel が描画される。

## 本 PR の対象外（別件）

- `default` namespace に残る INC-1 時の残骸 DaemonSet / Service / ServiceMonitor `grafana-alloy`
  （SA 不在で 0/7、FailedCreate 連発）の削除。Argo CD 管理外のため owner 承認後に手動削除。
- 「Loki への push が一定時間 0」を検知する PrometheusRule の追加。

## 再発防止テスト（chainsaw、PR #850 マージ後に追加）

owner 依頼により、kind 上で Alloy → Loki のログ収集経路を動的に検証する chainsaw suite を追加した。

- `tests/chainsaw/grafana-alloy/chainsaw-test.yaml`
  - `require-isolated-kind`: 他 suite と同じ隔離 kind ガード。
  - `verify-manifests-applied`: kustomize 適用済みの ConfigMap / ServiceMonitor
    （`app.kubernetes.io/part-of=kube-prometheus`）/ Application を確認。
  - `log-pipeline-end-to-end`: `log-pipeline.sh` を実行。
- `tests/chainsaw/grafana-alloy/log-pipeline.sh`
  1. 本番 `configmap.yaml` を適用し、`local.file_match "pods"` が `discovery.relabel.pods.output` を受け、
     `loki.source.file "pods"` の `targets` が `local.file_match.pods.targets` であることを静的に確認
     （`discovery.relabel.pods.output` を直接渡す旧配線は即 fail）。
  2. 使い捨て Loki 3.4.2（`loki-fixture.yaml`）を Service `monitoring/loki-gateway` として起動し、
     ConfigMap の push URL を変更せずに受け口を用意。
  3. `application.yaml` の chart repo / version（alloy 0.12.0）と本番 `helm/values.yaml` で
     `helm upgrade --install grafana-alloy` し、DaemonSet が全ノード（control-plane 含む）で Ready になることを確認。
  4. クラスター内 curl Pod から Loki に問い合わせ、全ノードについて `{node="<node>"}` が 300 秒以内に
     ストリームを返すこと、label に `namespace` / `pod` / `container` / `node` / `job` があること、
     `{namespace="monitoring"}` が返ることを確認。
  5. 各 Alloy Pod の `/metrics` で `loki_source_file_files_active_total > 0` と
     `loki_write_sent_entries_total > 0` を確認（本番で ServiceMonitor 経由で Prometheus に入る指標）。
- `.github/workflows/chainsaw.yaml`
  - `grafana-alloy` case を追加: ServiceMonitor CRD を本番 kube-prometheus
    （`argoproj/prometheus-operator/kustomization.yaml` の ref → `versions.json` の prometheusOperator）と
    同じ版から取得し、`kustomize build argoproj/grafana-alloy/` を適用。
  - ワークフロー自体の変更時に走る `required_components` に `grafana-alloy` を追加。
- `tests/workflows/test_chainsaw_isolation.py` の対象 suite に `grafana-alloy` を追加。

### ローカル検証（kind v0.33.0 / kindest/node:v1.37.0、2 ノード、chainsaw v0.2.15）

- 本番 ConfigMap: PASS（初回 95 秒、イメージキャッシュ後 47 秒）。
  control-plane で `files_active=11 / sent=977`、worker で `files_active=5 / sent=401`。
- negative control（ConfigMap を修正前の配線に戻し、静的チェックを外したスクリプト写しで実行）:
  Alloy は `failed to tail file, stat failed ... *.log: no such file or directory` を出し続け、
  Loki の label が空のまま 300 秒で `Loki にログが届いていないノード: ...` として FAIL。
  静的チェックを含む本来のスクリプトでは `alloy-config に local.file_match "pods" がありません` で即 FAIL。
- `python3 -B -m unittest discover -s tests/workflows`: 21 tests OK。
