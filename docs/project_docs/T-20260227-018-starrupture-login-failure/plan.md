# T-20260227-018: StarRupture ログイン不能事象 調査・復旧 Runbook

## 1. 事象サマリ

StarRupture dedicated server にプレイヤーがログインできない。
サーバーログに以下のエラーが繰り返し出力される。

```
LogRemoteControl: Error: Web Remote Call deserialization error: Object:
  /Game/Chimera/Maps/DedicatedServerStart.DedicatedServerStart:PersistentLevel
  .BP_DedicatedServerSettingsActor_C_1.DedicatedServerSettingsComp does not exist.
```

**発生環境:**
- Pod: `starrupture-694c99c778-4gpxz` / Node: `golyat-3`
- Image: `struppinet/starrupture-dedicated-server:latest`
- Namespace: `starrupture`
- PVC: `starrupture-saved-claim` (Longhorn, 5Gi)
- マウントパス: `/home/container/server_files/StarRupture/Saved/SaveGames`

## 2. 観測事実（Loki ログより）

### 2.1 エラーチェーン

サーバー起動時のログを時系列で追跡し、以下の因果関係を観測した。

```
[起動] DedicatedServerStart マップロード
   ↓
[エラー①] UCrDedicatedServerSettingsComp::LoadSavedGameLocal()
           - Save game doesn't exists
   ↓
[エラー②] LoadPackage: SkipPackage:
           .../DedicatedServerStart/BiomesWorldInfo
           - The package to load does not exist on disk or in the loader
   ↓
[結果] DedicatedServerSettingsComp が初期化されない
   ↓
[エラー③] クライアント接続時 Web Remote Call で
           DedicatedServerSettingsComp にアクセス → does not exist
   ↓
[症状] ログイン不能
```

### 2.2 同時発生: ファイルディスクリプタ枯渇

```
failed to create fsnotify watcher: too many open files
```

この警告が継続的に出力されており、ファイル操作の失敗がセーブデータ読み書きにも影響している可能性がある。

### 2.3 Wine 環境固有の制約

ログに `0154:err:eventlog:ReportEventW` 形式の出力があり、サーバーは **Wine/Proton 上で Windows バイナリとして動作** していることが確認された。Wine 環境では OS レベルの fd 制限がコンテナの `ulimit` と異なる形で適用される場合がある。

## 3. 原因仮説（優先度順）

### 仮説 1: セーブデータ不在（最有力）

**優先度: 高**

PVC マウントパス `/home/container/server_files/StarRupture/Saved/SaveGames` にセーブファイルが存在しない。

**根拠:**
- `UCrDedicatedServerSettingsComp::LoadSavedGameLocal() - Save game doesn't exists` が起動直後に記録
- T-20260225 で PVC mount failure が発生しており、Longhorn ボリュームの attach/detach 不整合が解消された際にデータが失われた可能性
- PVC は `ReadWriteOnce` かつ Longhorn バックエンドで、ボリューム復旧時にデータ整合性が保証されないケースがある

**切り分け方法:**
```bash
kubectl exec -n starrupture deploy/starrupture -- ls -la /home/container/server_files/StarRupture/Saved/SaveGames/
```

### 仮説 2: ファイルディスクリプタ制限超過

**優先度: 中**

コンテナの fd 上限が低く、UE サーバー + Wine のファイルハンドル要求を満たせていない。

**根拠:**
- `failed to create fsnotify watcher: too many open files` が継続的に発生
- fd 枯渇状態ではセーブファイルのオープン自体が失敗する
- UE 専用サーバーは多数のアセットファイルを同時オープンするため fd 消費が多い

**切り分け方法:**
```bash
# Pod 内の現在の fd 使用状況
kubectl exec -n starrupture deploy/starrupture -- cat /proc/1/limits | grep "open files"
kubectl exec -n starrupture deploy/starrupture -- ls /proc/1/fd | wc -l
```

### 仮説 3: Docker イメージのバージョン不整合

**優先度: 中**

`struppinet/starrupture-dedicated-server:latest` タグが更新され、セーブデータのフォーマットやコンポーネント構造が変更された。

**根拠:**
- `:latest` タグを使用しているため、Pod 再作成時に意図せず新バージョンが pull される
- UE のシリアライゼーションは型名に依存し、コンポーネントのリネーム/削除で `does not exist` エラーになる
- `BiomesWorldInfo` パッケージの不在もバージョン変更を示唆

**切り分け方法:**
```bash
# 現在のイメージダイジェストを確認
kubectl get pod -n starrupture -o jsonpath='{.items[*].status.containerStatuses[*].imageID}'
# Docker Hub の latest タグのダイジェストと比較
```

### 仮説 4: PVC マウント内容の不整合

**優先度: 低**

PVC は正常に Bound だが、Longhorn ボリュームの内部データが破損または不完全。

**根拠:**
- T-20260225 で Longhorn ボリュームの attach/detach 不整合が発生
- ボリュームの robustness は healthy だが、データレベルの整合性は別問題

**切り分け方法:**
```bash
# Longhorn UI でボリュームの詳細確認
# または
kubectl -n longhorn-system get volume pvc-0aea6d3c-017d-40d1-8f6e-7e43e9439a37 -o yaml
```

## 4. 即時復旧手順（短縮版）

> 上から順に試行し、各ステップ後にログインを再テストする。

### Step 0: PVC マウント状態の確認

まず PVC が正しくマウントされているかを確認する。マウントが異常な場合、セーブデータの有無確認が誤判定になる。

```bash
# Pod イベントの確認（マウント関連エラーがないか）
kubectl describe pod -n starrupture -l app=starrupture | grep -A 5 -E "(Events:|Mount|Volume)"

# PVC の状態確認
kubectl get pvc -n starrupture starrupture-saved-claim -o wide

# マウントポイントが実際にマウントされているか確認
kubectl exec -n starrupture deploy/starrupture -- findmnt /home/container/server_files/StarRupture/Saved/SaveGames
# findmnt が無い場合:
kubectl exec -n starrupture deploy/starrupture -- df /home/container/server_files/StarRupture/Saved/SaveGames
```

- マウントが正常 → Step 1 へ
- マウント異常（PVC 未 Bound / マウントエラー）→ T-20260225 の復旧手順を参照

### Step 1: セーブデータの状態確認

```bash
kubectl exec -n starrupture deploy/starrupture -- \
  ls -laR /home/container/server_files/StarRupture/Saved/SaveGames/
```

- ファイルが存在する → Step 3 へ
- ディレクトリが空 / 存在しない → Step 2 へ

### Step 2: サーバーの再起動（セーブデータ初期化）

セーブデータが無い場合、サーバーを再起動して初期セーブを生成させる。

```bash
# Pod を再起動（Deployment strategy: Recreate のため安全）
kubectl rollout restart deployment/starrupture -n starrupture
kubectl rollout status deployment/starrupture -n starrupture --timeout=300s
```

再起動後、ログを確認:
```bash
kubectl logs -n starrupture deploy/starrupture --tail=100 | grep -E "(SaveGame|DedicatedServer|Error)"
```

- `Save game doesn't exists` が再び出る場合 → Step 3 へ
- エラーが消えた → ログイン再テスト

### Step 3: fd 制限の確認と緩和

```bash
# 現在の制限を確認
kubectl exec -n starrupture deploy/starrupture -- cat /proc/1/limits
kubectl exec -n starrupture deploy/starrupture -- sh -c 'ls /proc/1/fd | wc -l'
```

fd が上限に近い場合、コンテナランタイムレベルで fd 上限を引き上げる必要がある（詳細は「5.2 fd 制限の引き上げ」参照）。**マニフェスト変更は別 PR で実施** すること。

### Step 4: イメージバージョンの固定と再デプロイ

```bash
# 現在動作中のイメージダイジェストを確認
kubectl get pod -n starrupture -o jsonpath='{.items[*].status.containerStatuses[*].imageID}'
```

バージョンを固定してデプロイし直す場合:
1. `deployment.yaml` の image を `struppinet/starrupture-dedicated-server:latest` から特定のダイジェスト/タグに変更
2. PR を作成して ArgoCD で sync

### Step 5: Longhorn バックアップからのリストア

上記すべてで解決しない場合、T-20260225 以前の Longhorn スナップショットからリストアを検討する。

**手順（安全な順序で実施）:**

```bash
# 1. アプリケーションを停止し、ボリュームへの書き込みを停止する
#    ※ GitOps (ArgoCD) 経由で deployment.yaml の replicas を 0 に変更し PR をマージ
#    または緊急時:
kubectl scale deployment/starrupture -n starrupture --replicas=0
kubectl rollout status deployment/starrupture -n starrupture --timeout=120s

# 2. Pod が完全に終了したことを確認
kubectl get pods -n starrupture -l app=starrupture
# (出力が空であること)

# 3. PVC のボリューム名を確認
VOLUME_NAME=$(kubectl get pvc -n starrupture starrupture-saved-claim -o jsonpath='{.spec.volumeName}')
echo "Volume: $VOLUME_NAME"

# 4. Longhorn UI でスナップショット一覧を確認
#    - Longhorn UI → Volumes → $VOLUME_NAME → Snapshots
#    - 正常時点（T-20260225 以前）のスナップショットを特定

# 5. スナップショットからリストア
#    - Longhorn UI → Volume → Revert to Snapshot
#    - ※ ボリュームが detached 状態であることを確認してから実行

# 6. リストア後のデータ検証
#    一時的に replicas=1 に戻してデータを確認
kubectl scale deployment/starrupture -n starrupture --replicas=1
kubectl rollout status deployment/starrupture -n starrupture --timeout=300s
kubectl exec -n starrupture deploy/starrupture -- \
  ls -laR /home/container/server_files/StarRupture/Saved/SaveGames/

# 7. ログインテスト実施
kubectl logs -n starrupture deploy/starrupture --tail=100 | grep -E "(SaveGame|DedicatedServer|Error)"
```

**リストア失敗時のロールバック:**
```bash
# リストアしたデータに問題がある場合、再度 replicas=0 にして
# 別のスナップショットで再試行するか、新規ワールドでの起動を検討する
kubectl scale deployment/starrupture -n starrupture --replicas=0
```

**注意:** `kubectl scale` による緊急変更は ArgoCD の selfHeal により元に戻される可能性がある。恒久的な変更は必ず Git リポジトリ経由で行うこと。

## 5. 恒久対策

### 5.1 イメージタグの固定（推奨度: 高）

**現状の問題:** `struppinet/starrupture-dedicated-server:latest` を使用しているため、Pod 再作成時に予期しないバージョン変更が発生する。

**対策:** 特定のバージョンタグまたはダイジェストを使用する。

```yaml
# deployment.yaml
image: struppinet/starrupture-dedicated-server:latest
# ↓ 以下のいずれかに変更
image: struppinet/starrupture-dedicated-server:<version-tag>
image: struppinet/starrupture-dedicated-server@sha256:<digest>
```

### 5.2 fd 制限の引き上げ（推奨度: 高）

Kubernetes の Pod spec / securityContext では `RLIMIT_NOFILE`（ulimit -n）を直接設定できない。fd 上限はコンテナランタイム（containerd / CRI-O）のデフォルト値に依存する。以下の方法で対応する。

**方法 A: コンテナランタイムのデフォルト fd 上限を引き上げ（推奨）**

ノードの containerd 設定で、コンテナのデフォルト `RLIMIT_NOFILE` を変更する。

```toml
# /etc/containerd/config.toml（ノード上）
[plugins."io.containerd.grpc.v1.cri"]
  [plugins."io.containerd.grpc.v1.cri".containerd]
    [plugins."io.containerd.grpc.v1.cri".containerd.default_runtime]
      [plugins."io.containerd.grpc.v1.cri".containerd.default_runtime.options]
        # デフォルトの RLIMIT_NOFILE を引き上げ
```

設定変更後、containerd の再起動が必要。影響範囲がノード全体に及ぶため、StarRupture が動作するノードに限定して適用することを推奨。

**方法 B: Docker イメージの entrypoint で ulimit を設定**

イメージの entrypoint スクリプトで `ulimit -n 65536` を実行する。ただし `struppinet/starrupture-dedicated-server` は外部提供イメージのため、カスタム entrypoint ラッパーを作成する必要がある。

```yaml
# deployment.yaml で command を上書き
command: ["/bin/sh", "-c"]
args: ["ulimit -n 65536 && exec /original-entrypoint.sh"]
```

**方法 C: ノードの sysctl で fs.inotify 上限を引き上げ**

`too many open files` が inotify watcher 不足に起因する場合は、ノードの sysctl を調整する。

```bash
# ノード上で実行
sysctl -w fs.inotify.max_user_watches=524288
sysctl -w fs.inotify.max_user_instances=1024
```

または Kubernetes の Pod spec で `securityContext.sysctls` を使用（安全な sysctl に限る）。

### 5.3 セーブデータのバックアップ自動化（推奨度: 中）

Longhorn の定期スナップショット / バックアップポリシーを設定する。

```yaml
# Longhorn RecurringJob の例
apiVersion: longhorn.io/v1beta2
kind: RecurringJob
metadata:
  name: starrupture-backup
  namespace: longhorn-system
spec:
  cron: "0 */6 * * *"  # 6時間ごと
  task: backup
  retain: 7
  concurrency: 1
  labels:
    recurring-job.longhorn.io/source: enabled
    recurring-job.longhorn.io/group: starrupture
```

### 5.4 起動時ヘルスチェックの追加（推奨度: 中）

Deployment に `readinessProbe` / `livenessProbe` を設定し、サーバーが正常にクライアントを受け付けられる状態であることを確認する。

`tcpSocket` による probe はポートが listen しているかのみを確認するため、アプリケーション層の健全性（サーバー設定の初期化完了、ログイン受け付け可能な状態）の保証としては不十分である。ただし StarRupture の専用サーバーは HTTP ヘルスエンドポイントを提供していないため、現時点では `tcpSocket` が実用的な選択肢となる。

```yaml
# deployment.yaml に追加
readinessProbe:
  tcpSocket:
    port: 7777
  initialDelaySeconds: 120
  periodSeconds: 10
  failureThreshold: 6
livenessProbe:
  tcpSocket:
    port: 7777
  initialDelaySeconds: 180
  periodSeconds: 30
  failureThreshold: 5
```

将来的にサーバー側で HTTP ヘルスエンドポイントや UE Remote Control API の応答確認が可能になった場合は、`httpGet` ベースの probe に移行することを推奨する。

### 5.5 監視・アラートの追加（推奨度: 中）

Loki ログベースのアラートルールを追加し、`DedicatedServerSettingsComp does not exist` や `too many open files` の検出時に通知する。

```yaml
# Prometheus/Loki alert rule の例
groups:
- name: starrupture
  rules:
  - alert: StarRuptureLoginFailure
    expr: |
      count_over_time({namespace="starrupture"} |= "DedicatedServerSettingsComp does not exist" [5m]) > 0
    for: 5m
    labels:
      severity: critical
    annotations:
      summary: "StarRupture サーバーでログイン不能エラーが発生"
  - alert: StarRuptureFdExhaustion
    expr: |
      count_over_time({namespace="starrupture"} |= "too many open files" [5m]) > 5
    for: 5m
    labels:
      severity: warning
    annotations:
      summary: "StarRupture サーバーでファイルディスクリプタ枯渇"
```

## 6. 関連インシデント

| チケット | 事象 | 関連性 |
|----------|------|--------|
| T-20260225 | PVC mount failure (Longhorn volume attach/detach 不整合) | PVC 障害後にセーブデータが失われた可能性が高い。直接の前因事象。 |

## 7. 技術的補足

### サーバーアーキテクチャ

- **ゲームエンジン:** Unreal Engine ベース
- **実行環境:** Wine/Proton 上で Windows バイナリを実行（`0154:err:eventlog:ReportEventW` ログ形式から確認）
- **サーバーマップ:** `/Game/Chimera/Maps/DedicatedServerStart`
- **設定コンポーネント:** `BP_DedicatedServerSettingsActor_C_1.DedicatedServerSettingsComp`（UE ActorComponent）
- **Web API:** UE Remote Control Plugin（JSON over HTTP でエンジンオブジェクトにアクセス）

### DedicatedServerSettingsComp の役割

`UCrDedicatedServerSettingsComp` はサーバー設定を保持する UE コンポーネント。起動時に `LoadSavedGameLocal()` でセーブファイルからサーバー設定（ワールドシード、ルール、プレイヤーデータ等）を読み込む。このコンポーネントが初期化に失敗すると、Remote Control API 経由のクライアント認証・設定取得が不可能になり、ログインが失敗する。

### 環境変数 `USE_DSSETTINGS=true` の意味

Deployment で設定されている `USE_DSSETTINGS: "true"` は、サーバーが `DedicatedServerSettingsComp` を使用してサーバー設定を管理することを示す。この設定が `true` の場合、セーブファイルが存在しないとコンポーネントの初期化が不完全になる。
