# T-20260228-005: StarRupture Manage Server 不安定回避の運用固定

> **重要: Manage Server（Web UI）は本番運用で禁止（例外条件なし）。**
> サーバー設定は DSSettings.txt で管理し、プレイヤーは Join Game（IP直接接続）で参加すること。

## 1. 背景

StarRupture Dedicated Server において、Manage Server（ブラウザ経由のサーバー管理UI）から「ゲームをスタート」を押しても無反応になる問題が発生している。
サーバーログには以下のエラーが記録される。

```
LogRemoteControl: Error: Web Remote Call deserialization error: Object:
  /Game/Chimera/Maps/DedicatedServerStart.DedicatedServerStart:PersistentLevel
  .BP_DedicatedServerSettingsActor_C_1.DedicatedServerSettingsComp does not exist.
```

## 2. 症状・発生条件

### 症状
- Manage Server（Web UI）で「ゲームをスタート」ボタンを押しても反応がない
- サーバーログに `DedicatedServerSettingsComp does not exist` が繰り返し出力される
- プレイヤーがゲームクライアントからログインできない

### 発生条件
- Manage Server（UE Remote Control Plugin 経由の Web UI）を使用してサーバーを操作しようとした場合
- サーバー起動時にセーブデータが存在しない、またはロードに失敗した場合
- PVC マウント障害後のサーバー再起動時

## 3. 原因仮説

Manage Server は UE Remote Control Plugin を通じて `DedicatedServerSettingsComp`（UE ActorComponent）にアクセスする。
このコンポーネントは `LoadSavedGameLocal()` で起動時にセーブファイルを読み込むが、以下のいずれかの理由で初期化に失敗する場合がある。

1. **セーブデータ不在**: PVC マウントパスにセーブファイルがない
2. **ファイルディスクリプタ枯渇**: Wine/Proton 環境下でfd上限に到達
3. **イメージバージョン不整合**: `:latest` タグの予期しない更新

初期化に失敗すると `DedicatedServerSettingsComp` がワールドに存在しない状態となり、
Remote Control API 経由のあらゆる操作（Manage Server 含む）が `does not exist` エラーを返す。

## 4. 回避策: DSSettings.txt 主導運用

Manage Server は使用禁止とし、**DSSettings.txt** でサーバー設定を管理する。
プレイヤーは **Join Game（IP直接接続）** で参加する運用に固定する。

### 環境変数設定

`deployment.yaml` の環境変数 `USE_DSSETTINGS` を `true` に設定する必要がある。

```yaml
env:
- name: USE_DSSETTINGS
  value: "true"    # DSSettings.txt ベースの運用（必須）
```

> **注意**: 本 PR にて `deployment.yaml` の `USE_DSSETTINGS` を `false` → `true` に変更済み。

### 環境変数の確認方法
```bash
# 実行中の Pod で USE_DSSETTINGS の実効値を確認
kubectl exec -n starrupture deploy/starrupture -- printenv USE_DSSETTINGS
```

### プレイヤーの接続方法
1. ゲームクライアントを起動
2. 「Join Game」を選択
3. サーバーIP `192.168.10.31` ポート `7777` を入力して接続

### DSSettings.txt の配置
サーバーコンテナ内の以下のパスに DSSettings.txt が自動生成される:
```
/home/container/server_files/StarRupture/Saved/Config/LinuxServer/DSSettings.txt
```

## 5. 初期セットアップ手順（新規ワールド開始）

### Step 1: デプロイメント確認
```bash
# Pod が Running であることを確認
kubectl get pods -n starrupture -l app=starrupture

# PVC がマウントされていることを確認
kubectl get pvc -n starrupture
```

### Step 2: サーバー起動・初回セーブ生成
```bash
# ログを確認してサーバーが正常に起動したことを確認
kubectl logs -n starrupture deploy/starrupture --tail=50

# セーブディレクトリの状態を確認
kubectl exec -n starrupture deploy/starrupture -- \
  ls -la /home/container/server_files/StarRupture/Saved/SaveGames/
```

### Step 3: ゲーム内での初期設定
1. Join Game で `192.168.10.31:7777` に接続
2. サーバーに入ったらワールド設定を確認
3. 一旦ログアウトして再度ログインし、接続が安定することを確認

### Step 4: セーブデータの保存確認
```bash
# セーブファイルが生成されていることを確認
kubectl exec -n starrupture deploy/starrupture -- \
  ls -laR /home/container/server_files/StarRupture/Saved/SaveGames/

# ファイルサイズが0でないことを確認
kubectl exec -n starrupture deploy/starrupture -- \
  du -sh /home/container/server_files/StarRupture/Saved/SaveGames/
```

### Step 5: 再起動テスト
```bash
# Pod を再起動
kubectl rollout restart deployment/starrupture -n starrupture
kubectl rollout status deployment/starrupture -n starrupture --timeout=300s

# 再起動後のログ確認（DedicatedServerSettingsComp エラーがないこと）
kubectl logs -n starrupture deploy/starrupture --tail=100 | \
  grep -E "(SaveGame|DedicatedServer|Error)"
```

### Step 6: 以後の参加手順
通常のプレイヤー参加は以下のみ:
1. ゲームクライアント起動
2. 「Join Game」→ `192.168.10.31:7777`
3. **Manage Server（Web UI）は使用禁止** — いかなる場合も使用しないこと

## 6. トラブルシュート

### 6.1 TCP/UDP 疎通確認
```bash
# Service/Endpoint の状態確認
kubectl get svc -n starrupture
kubectl get endpoints -n starrupture

# 外部からの疎通確認（クラスタ内のNodeから）
nc -zvu 192.168.10.31 7777   # UDP
nc -zv 192.168.10.31 7777    # TCP
```

### 6.2 Pod/Service 状態確認
```bash
# Pod の詳細状態
kubectl describe pod -n starrupture -l app=starrupture

# Service の詳細（port/targetPort/protocol 不整合確認）
kubectl describe svc starrupture -n starrupture

# イベント確認
kubectl get events -n starrupture --sort-by='.lastTimestamp'

# PVC マウント状態
kubectl exec -n starrupture deploy/starrupture -- \
  df /home/container/server_files/StarRupture/Saved/SaveGames

# USE_DSSETTINGS 環境変数の確認（true であること）
kubectl exec -n starrupture deploy/starrupture -- printenv USE_DSSETTINGS
```

### 6.3 イメージバージョン確認
```bash
# 現在動作中のイメージダイジェストを確認
kubectl get pod -n starrupture -l app=starrupture \
  -o jsonpath='{.items[*].status.containerStatuses[*].imageID}'

# イメージタグの確認
kubectl get deployment starrupture -n starrupture \
  -o jsonpath='{.spec.template.spec.containers[0].image}'
```

> `:latest` タグを使用しているため、Pod 再作成時に意図しないバージョン変更が起きる可能性がある。
> 問題が疑われる場合はダイジェストで固定するか、既知の安定バージョンタグを指定する。

### 6.4 ログ確認
```bash
# 直近のサーバーログ
kubectl logs -n starrupture deploy/starrupture --tail=200

# エラーのみ抽出
kubectl logs -n starrupture deploy/starrupture --tail=500 | \
  grep -iE "(error|fatal|fail|does not exist)"

# DedicatedServerSettingsComp 関連
kubectl logs -n starrupture deploy/starrupture --tail=500 | \
  grep "DedicatedServerSettingsComp"

# fd 枯渇の確認
kubectl logs -n starrupture deploy/starrupture --tail=500 | \
  grep "too many open files"
```

### 6.5 `DedicatedServerSettingsComp does not exist` が発生した場合の即応手順

1. **セーブデータの有無を確認**
   ```bash
   kubectl exec -n starrupture deploy/starrupture -- \
     ls -laR /home/container/server_files/StarRupture/Saved/SaveGames/
   ```

2. **セーブデータがない場合**: Pod を再起動後、Join Game で接続して初期セーブを生成させる
   ```bash
   kubectl rollout restart deployment/starrupture -n starrupture
   kubectl rollout status deployment/starrupture -n starrupture --timeout=300s
   # 再起動後、Join Game で 192.168.10.31:7777 に接続して初回セーブを生成
   # セーブが生成されたことを確認:
   kubectl exec -n starrupture deploy/starrupture -- \
     ls -laR /home/container/server_files/StarRupture/Saved/SaveGames/
   ```

3. **セーブデータがある場合**: fd 枯渇や PVC マウント問題を疑う
   ```bash
   kubectl exec -n starrupture deploy/starrupture -- cat /proc/1/limits | grep "open files"
   kubectl exec -n starrupture deploy/starrupture -- sh -c 'ls /proc/1/fd | wc -l'
   ```

4. **上記で解決しない場合**: Longhorn スナップショットからのリストアを検討
   → [T-20260225 PVC Investigation](../T-20260225-starrupture-pvc-investigation/plan.md) を参照

## 7. 関連ドキュメント

| チケット | 内容 |
|----------|------|
| T-20260225 | PVC Mount Failure Investigation（Longhorn ボリューム attach/detach 不整合） |
| T-20260227-018 | StarRupture ログイン不能事象 調査・復旧 Runbook（未マージ） |

## 8. 変更内容サマリ

| 変更対象 | 内容 |
|----------|------|
| `docs/project_docs/T-20260228-005-starrupture-manage-server-runbook/plan.md` | 本ドキュメント（運用手順書） |
| `argoproj/starrupture/README.md` | Manage Server 依存禁止の注記を追加 |
| `argoproj/starrupture/deployment.yaml` | `USE_DSSETTINGS` を `false` → `true` に変更 |
