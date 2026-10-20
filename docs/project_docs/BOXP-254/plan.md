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
    （`app.kubernetes.io/part-of=kube-prometheus`）を確認。
  - `log-pipeline-end-to-end`: `log-pipeline.sh` を実行。
  - `apply-application-yaml`: 最後に `application.yaml` を適用（kube-vip / ESO suite と同じ）。
    先に適用すると、argocd suite が起動した本物の Argo CD が automated sync で Alloy chart を
    展開してしまい、`helm install` と ServiceAccount 等の所有権が衝突する（PR #853 の CI 2 回目で発生）。
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
    同じ版から取得し、`kustomize build argoproj/grafana-alloy/` から Application を除いて適用。
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

### CI（PR #853）

- 1 回目: grafana-alloy suite は Calico 環境でも PASS（67 秒、`files_active` 31 / 18、`sent` 7863 / 4104）。
  ジョブ全体は、ワークフロー変更時に同時実行される既存 argocd suite が `public.ecr.aws` の
  `redis:8.2.3-alpine` を pull できず（429 Data limit exceeded）失敗。本変更とは無関係。
- 2 回目（rerun）: argocd suite が通り本物の Argo CD が動いた結果、事前適用していた
  `grafana-alloy` Application を Argo CD が同期して Alloy を展開し、suite 内の `helm install` が
  `ServiceAccount "grafana-alloy" ... cannot be imported into the current release` で失敗。
  Application の適用を suite 末尾へ移して修正。

## chainsaw 実行時間の短縮（owner レビュー指摘: 「テスト実行時間が長過ぎる、並列実行できないか」）

PR #853 の CI 3 回目（7m25s）の内訳は、kind 作成 97s / Calico 52s / 事前適用 50s / suite 実行 223s で、
suite 実行は argocd 135s → ESO 7s → grafana-alloy 74s → kube-vip 6s の直列だった。

### 変更

1. `.github/workflows/chainsaw.yaml` の `Run chainsaw tests`
   - component ごとの `chainsaw test` をバックグラウンドで同時に起動し、`wait` で個別に終了コードを回収する。
   - 出力は `$RUNNER_TEMP/chainsaw-output/<component>.txt` に分け、PR コメント用の結果は
     完了順ではなく COMPONENTS の順で組み立てる（コメントの体裁は従来どおり）。
   - 各 suite は namespace が分かれており（argocd / external-secrets / monitoring / kube-system）、
     互いの結果に依存しない。Application の適用は従来どおり各 suite の末尾なので、本物の Argo CD との
     所有権衝突（CI 2 回目の件）も変わらない。
   - preflight workflow（`chainsaw-preflight.yaml`）は matrix で component ごとに job が分かれており
     既に並列なので変更なし。
2. `tests/chainsaw/grafana-alloy/log-pipeline.sh`
   - Loki fixture / Alloy chart / curl Pod を先に全部作ってから待つ（イメージ pull と起動を重ねる）。
   - `helm --wait` をやめ `kubectl rollout status daemonset` で待つ。helm の DaemonSet 待ちは
     `maxUnavailable`（既定 1）を差し引くため 2 ノードで 1 Pod Ready の時点で戻り、
     直後の `ready == desired` 検査が落ちる flaky 要因だった（ローカル baseline で実際に 1/2 Ready で FAIL）。
   - ログ到達のポーリング間隔を 10s → 3s。
3. `tests/chainsaw/argocd/network-policy-impact.sh`
   - probe Pod 9 個を先に全部作ってから待つ。
   - `terminationGracePeriodSeconds: 1`。probe の python が PID 1 で SIGTERM を無視し、
     末尾の namespace 削除が grace period 30s を使い切っていた（ローカル計測で削除に 43s）。
   - blocked 判定 4 件（1 件 6s 以上）を同時に実行し結果を個別に回収する。reachable 判定は従来どおり直列。
   - 判定内容（許可 peer / 同 ns 非許可 / 別 ns spoof / Tunnel proxy）は変更していない。

### ローカル計測（kind v0.33.0 + Calico v3.33.0、2 ノード、CI と同じ事前適用）

| | 変更前（直列） | 変更後（並列） |
| --- | --- | --- |
| argocd | 142s（NetworkPolicy matrix 93s） | 104s（matrix 55s） |
| grafana-alloy | 74s（helm --wait が 1/2 Ready で戻り FAIL） | 92s（並列でイメージ pull が競合、PASS） |
| external-secrets-operator | 6s | 6s |
| kube-vip | 6s | 6s |
| suite 実行合計 | 229s | 104s |

CI では suite 実行 223s → 約 105s、ジョブ全体 7m25s → 約 5m30s を見込む。
残りは kind 作成 + Calico + 事前適用（約 3.3 分）で、hosted runner ではイメージキャッシュが効かないため
これ以上は構成変更（Calico を不要にする、kind ノード image の事前 pull 等）が必要。

### CI 実測（PR #853、run 39426040453）

- 1 回目は argocd suite が `public.ecr.aws` の `redis:8.2.3-alpine` pull で 429 Too Many Requests
  （CI 1 回目と同じ外部レート制限）となり失敗。rerun で 4 suite PASS。
- rerun: ジョブ全体 4m56s（変更前 7m25s）、`Run chainsaw tests` 71s（変更前 223s）。
  argocd 71s / grafana-alloy 53s / ESO 8s / kube-vip 8s を並列実行。

## preflight workflow への grafana-alloy 追加（codex-review 指摘対応）

codex-review の指摘: `chainsaw-preflight.yaml`（Kubernetes 更新前の隔離 kind 検証）の matrix が
`argocd / external-secrets-operator / kube-vip` のままで、追加した `grafana-alloy` suite が
preflight では一度も実行されない。

### 変更（`.github/workflows/chainsaw-preflight.yaml`）

- matrix `component` に `grafana-alloy` を追加（current / candidate × 4 component = 8 job）。
- `Prepare selected component in isolated kind` に `grafana-alloy` の case を追加:
  - ServiceMonitor CRD は本番の kube-prometheus（`argoproj/prometheus-operator/kustomization.yaml` の
    `ref=v0.19.0`）が使う prometheus-operator **0.94.1** に固定し、CRD ファイルの sha256
    （`31bdff39…9769`）を ESO chart / argocd install.yaml と同じ方式で検証する。
    kube-prometheus の `versions.json` から取れる `prometheusOperator` が固定版と異なれば失敗させ、
    版と digest を同時に更新する運用にする（通常 CI の `chainsaw.yaml` は versions.json から動的に取得）。
  - `monitoring` namespace を作成し、`kustomize build argoproj/grafana-alloy/` から Application を除いて適用。
    Application は通常 CI と同じく suite 末尾の `apply-application-yaml` で適用する。
  - Alloy 本体の Helm install と使い捨て Loki は従来どおり `log-pipeline.sh` が行う（preflight job には
    `azure/setup-helm` v4.3.1 が入っている）。
- evidence に `kube-prometheus-versions.json` / `prepared-servicemonitor-crd.yaml` /
  `servicemonitor-crd-{expected,actual}.sha256` / `prepared-grafana-alloy.yaml` を追加。

### ローカル検証（preflight と同構成: kind v0.33.0 / kindest/node:v1.37.0、1 ノード、既定 CNI）

- workflow から抽出した prepare step を `COMPONENT=grafana-alloy` で実行し exit 0（CRD digest 一致、
  versions.json の prometheusOperator = 0.94.1）。
- `chainsaw test --report-format JUNIT-TEST` で suite PASS（58s、`loki_source_file_files_active_total` 13、
  `loki_write_sent_entries_total` 611）、report gate（PYREPORT）も exit 0。
- `tests/workflows` の unittest 21 件 OK。
- preflight は `workflow_dispatch` 専用のため、本 PR の CI では `chainsaw.yaml` 側（4 suite）のみ走る。

## chainsaw CI の固定費削減（owner 追加レビュー: 「6m5s はまだ長い」への対応）

### 計測（PR #853 の chainsaw ジョブ 7 run、GitHub hosted runner、step 別）

| run | 内容 | kind 作成 | Calico | 事前適用 | suite 実行 | ジョブ全体 |
| --- | --- | --- | --- | --- | --- | --- |
| 39402487022 | ESO のみ（renovate） | 93s | 44s | 42s | 6s | 3m22s |
| 39409991582 | 4 suite 直列（高速化前） | 97s | 52s | 50s | 223s | 7m25s |
| 39426040453 #1 | 4 suite 並列 | 95s | 52s | 48s | 347s（redis 429 で FAIL） | 9m24s |
| 39426040453 #2 | 同 rerun | 99s | 55s | 45s | 71s | 4m56s |
| 39428255488 | docs のみ変更 | 95s | 53s | 113s | 81s | 6m4s |
| 39430340382 | preflight 追加 | 116s | 56s | 96s | 72s | 6m5s |

- 4m56s → 6m5s の差は suite ではなく **事前適用（45s → 96〜113s）**。内訳は ESO の `helm install --wait` が
  28s → 79s（ghcr.io からの image pull 速度）で、本 PR の変更内容とは無関係のばらつき。
- rerun / 429 の影響は suite 実行（347s）にだけ現れ、環境構築には影響しない。
- 固定費: kind 作成 93〜116s のうち **60s は kind-action の既定 `wait: 60s`** で、kind-config が
  `disableDefaultCNI: true` のため Calico 導入まで node が Ready にならず毎回タイムアウト
  （ログの `✗ Waiting ≤ 1m0s for control-plane = Ready`）していた純粋な無駄。
- Calico 44〜56s と事前適用 42〜113s は直列で、どちらもコンテナイメージの pull 待ちが大半。

### 変更（`.github/workflows/chainsaw.yaml`、`tests/workflows/kind-ci-images.sh`、`tests/workflows/kind-prefetch-images.sh`）

1. kind-action に `wait: 0s`。node の Ready は Calico 導入後に `kubectl wait node --all` で待つ（検証内容は同じ、無駄な 60s だけ消える）。
2. "Install Calico" step は CRD と tigera-operator を作るだけにし（1 秒）、`kubectl wait` 群は
   "Apply component manifests" の後半へ移動して、CRD / Helm release / kustomize の適用を Calico 起動と並行させる。
   - tigera-operator は起動後に operator.tigera.io の CRD を自分で作るため、`custom-resources.yaml`
     （Installation 等）は Apply step 冒頭のバックグラウンドで CRD を待ってから作成する。
3. 新 step "Build manifests and prefetch container images": まず各 component の `kustomize build` を
   1 回だけ行い `$RUNNER_TEMP/manifests/<component>.yaml` に残す（Apply step はこのファイルを使う）。
   その後バックグラウンドでイメージを**ノード内 containerd へ先行 pull**（`kind-ci-images.sh` が
   ビルド済みマニフェスト / helm template / suite の fixture からイメージを列挙し、`kind-prefetch-images.sh` が
   `docker exec <node> ctr -n k8s.io images pull` を並列実行）。
   - kustomize build を pull 開始前に済ませるのは、argocd base など remote resource の取得（kustomize 内部の
     27s タイムアウト）がイメージ pull と帯域を取り合うのを避けるため。ローカル（低帯域）では pull と同時だと
     argo-cd の shallow fetch が単独 6s → 27s 超で 3 回連続失敗した。remote 取得は 3 回まで再試行する。
   - DaemonSet / CNI のイメージは全ノード、それ以外は worker だけ（kind は control-plane に NoSchedule taint）。
   - best effort: 取れなくても exit 0 で、kubelet が通常どおり pull する。429 などは 3 回までリトライ。
   - cache は使わない（run ごとに空のノードへ pull）。cold / warm の区別がなく、PR 間で共有する状態もないため
     未信頼 PR による cache 汚染の問題は生じない。
4. ESO の `helm install --wait` をやめ、`external-secrets.io` 系リソース（argocd の ExternalSecret ×3、
   ESO overlay の ClusterSecretStore）だけを ESO Deployment Available 後に適用する（validating webhook 依存）。
   それ以外の argocd マニフェストは Calico を待たずに apply し、Pod 作成とイメージ pull を先に進める。
5. 変更していないもの: suite の検証内容・timeout・JUnit 集計・failure 伝播、component 選択
   （workflow / `tests/workflows` 変更時は 4 suite 全件）、Calico（NetworkPolicy 検証のため常に導入）。

### ローカル計測（kind v0.33.0 + Calico v3.33.0、2 ノード、2 vCPU・低帯域のホスト、4 suite、workflow から step を抽出して実行）

| | 変更前 | 変更後 |
| --- | --- | --- |
| kind 作成 | 78s（`✗ Waiting ≤ 1m0s` 込み） | 21s |
| Calico step | 84s | 2s（リソース作成のみ） |
| マニフェスト build + pre-pull 起動 | – | 17s（argo-cd の remote 取得込み） |
| 事前適用（Calico / ESO 待ち込み） | 80s | 139s（pre-pull 24 件すべて成功、最長は calico/node 81s） |
| suite 実行（並列） | 137s（argocd 137s / grafana-alloy 95s） | 69s（argocd 70s / grafana-alloy 32s） |
| 合計 | 381s | 249s |

- 4 suite とも PASS。suite の内容は変更していないので、旧配線の negative control（PR #853 初回で確認した
  `loki.source.file` に glob を直接渡す設定が 300s で FAIL する挙動）はそのまま。
- このホストは帯域が狭く pull 時間が CI より大きいため、絶対値は CI の参考にならない。差分の内訳
  （kind -57s、suite -68s、Calico+事前適用 -25s）は CI でも同方向に出る見込み。

### CI 実測（PR #853、run 39460101502、commit 9ee88d5、4 suite 全件、1 回目で PASS）

| step | 変更前（run 39430340382） | 変更後 |
| --- | --- | --- |
| Create kind cluster | 116s | 34s |
| Install Calico | 56s | 2s（リソース作成のみ） |
| Build manifests and prefetch（前景部分） | – | 7s |
| Apply component manifests | 96s | 87s（内訳: 適用 25s → Calico 待ち 31s → ESO Available 待ち 31s） |
| Run chainsaw tests | 72s | 62s（argocd 61s / grafana-alloy 44s / ESO 7s / kube-vip 7s） |
| ジョブ全体 | 6m5s | **3m34s** |

- pre-pull は 24 件すべて成功（43s、最長は alloy 21s / argocd 18s）。redis（public.ecr.aws）も 13s で取得でき
  429 は出ていない（run ごとの pull 回数は変えていないので、発生確率も変わらない）。
- 残りの固定費: kind 34s + Calico 起動 31s + ESO 起動 31s（argocd / ESO を含む場合のみ）。
  component 単体（grafana-alloy / kube-vip）の PR なら ESO 待ちが無く、kind 34s + 環境 40s + suite 45s +
  overhead 15s ≈ **2 分台前半の見込み**（本 PR は workflow 変更のため全件実行しか走らず、単体は未実測）。
- 4 suite 全件（workflow / `tests/workflows` 変更時）は 3.5 分前後。これ以上は Calico と ESO の起動
  （どちらもイメージは取得済みで、プロセス起動と readiness の時間）か、suite 内の待ち
  （argocd: Deployment 起動 + NetworkPolicy matrix 約 30s、grafana-alloy: Loki への到達待ち）を削る必要がある。
