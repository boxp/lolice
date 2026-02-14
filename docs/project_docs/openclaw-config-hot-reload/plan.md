# OpenClaw Config ホットリロード機能の実装計画

## 目的

OpenClawのConfigMap更新時にPodが再生成（Recreate）されるのを抑制し、OpenClawが内蔵するchokidarベースのファイル監視によるホットリロード機能を活用する。これにより、設定変更時のダウンタイムをゼロにする。

## 背景

- 現在、Stakater ReloaderのアノテーションによりConfigMap変更時にPodが再生成される
- OpenClawはchokidar（300msデバウンス）でファイル変更を自動検知し、ホットリロードする機能を持っている
- ConfigMapのマウントはKubernetesがシンボリックリンクで管理するため、chokidarが直接検知できない場合がある
- サイドカーでConfigMap→PVC間のコピーを行うことで、確実にホットリロードを実現する

## 現状の問題

### Stakater Reloaderによる不要なPod再生成

```
現在のフロー:
  Git push → ArgoCD sync → ConfigMap更新
    → Stakater Reloader検知
      → Pod再生成（Recreate戦略 = 旧Pod停止 → 新Pod起動）
        → ダウンタイム発生（数十秒〜数分）
```

**影響:**
- 設定変更のたびにOpenClawプロセスが完全再起動
- 進行中のタスク、セッション、接続が全て切断
- initContainer（init-config, init-dotfiles）が毎回再実行される

### ConfigMapのシンボリックリンク問題

KubernetesはConfigMapをPod内に `..data` → `..timestamp` シンボリックリンク経由でマウントする。chokidarはシンボリックリンクの差し替えを検知できない場合があるため、ConfigMapを直接watchする方式は信頼性が低い。

## 修正後のフロー

```
Git push → ArgoCD sync → ConfigMap更新
                            ↓
              config-manager-sidecar (60秒ポーリング)
                            ↓
              md5sumでハッシュ比較 → 変更検知
                            ↓
              原子的コピー (tmp → mv) でPVC上のopenclaw.json更新
                            ↓
              chokidar検知 (300msデバウンス)
                            ↓
              OpenClawホットリロード → ダウンタイムなし
```

**最大設定反映遅延:** 約2〜3分（ConfigMap伝搬 ~60秒 + ポーリング ~60秒 + chokidar ~300ms）

## 変更内容

### 1. `deployment-openclaw.yaml` の修正

- **Reloaderアノテーション削除**: `configmap.reloader.stakater.com/reload` アノテーションを完全に削除（ConfigMap変更でPod再生成させない）
- **config-manager-sidecar追加**: ConfigMap変更を定期的にチェックし、変更があればPVCに原子的コピーするサイドカーコンテナを追加

### 2. `configmap-openclaw-cron.yaml` の新規作成

- OpenClawのcronジョブ設定をGitOpsで管理するためのConfigMap
- `crontab.json` にcronジョブ定義を格納
- 将来的にcronジョブ設定もホットリロード対象にできるよう準備

### 3. `kustomization.yaml` の修正

- `configmap-openclaw-cron.yaml` をリソースに追加

## 実装詳細

### config-manager-sidecar

```yaml
- name: config-manager
  image: ghcr.io/boxp/arch/openclaw:<current-tag>
  command: ["sh", "-c"]
  args:
    - |
      LAST_HASH=$(md5sum /home/node/.openclaw/openclaw.json 2>/dev/null | cut -d' ' -f1)
      while true; do
        CURRENT_HASH=$(md5sum /config/openclaw.json 2>/dev/null | cut -d' ' -f1)
        if [ -n "$CURRENT_HASH" ] && [ "$CURRENT_HASH" != "$LAST_HASH" ]; then
          cp /config/openclaw.json /home/node/.openclaw/openclaw.json.tmp && \
            mv /home/node/.openclaw/openclaw.json.tmp /home/node/.openclaw/openclaw.json
          echo "$(date -Iseconds) config synced (hash=$CURRENT_HASH)"
          LAST_HASH="$CURRENT_HASH"
        fi
        sleep 60
      done
```

**ポイント:**
- 起動時にPVC上の既存ファイルのハッシュを取得（初回の不要なコピーを防止）
- `cp + mv` による原子的書き込み（部分書き込みによる破損を防止）
- `2>/dev/null` でファイル不在時のエラーを抑制
- 変更検知時にタイムスタンプ付きログを出力（デバッグ用）

### セキュリティ

- サイドカーはメインコンテナと同等のsecurityContext（非特権、runAsUser: 1000）
- ConfigMapボリュームはreadOnlyでマウント
- リソース制限は最小限（CPU: 10m〜50m, Memory: 32Mi〜64Mi）
- 全ケイパビリティをドロップ

### ホットリロード対応設定項目

以下の設定はPod再起動なしでホットリロード可能:
- `channels.*` (Discord設定)
- `agents` (モデル設定、エイリアス)
- `models` (プロバイダー設定)
- `hooks`, `cron`, `session`, `messages`
- `tools`, `browser`, `skills`, `audio`, `talk`, `ui`, `logging`

以下の設定変更時はPod再起動が必要（OpenClawの仕様）:
- `gateway.port`, `gateway.bind`, `gateway.auth`
- `gateway.tailscale`, `plugins`, `discovery`

## テスト計画

### デプロイ前確認

1. **マニフェストの静的検証**
   ```bash
   kubectl kustomize argoproj/openclaw/ --enable-helm 2>&1
   ```
   - YAML構文エラーがないこと
   - リソース参照が正しいこと

2. **ArgoCD Dry Run**
   - ArgoCD UIで `openclaw` Application のdiffを確認
   - 期待する変更（Reloaderアノテーション削除、サイドカー追加）のみであること

### デプロイ後確認

3. **Pod再生成されないことの確認**
   ```bash
   # デプロイ前にPod名とAGEを記録
   kubectl get pod -n openclaw -l app.kubernetes.io/name=openclaw
   # ConfigMap変更後にPod名とAGEが変わらないことを確認
   kubectl get pod -n openclaw -l app.kubernetes.io/name=openclaw
   ```

4. **サイドカーの動作確認**
   ```bash
   # config-managerコンテナのログ確認
   kubectl logs -n openclaw deploy/openclaw -c config-manager -f
   ```
   - ConfigMap変更後、60秒以内に `config synced` ログが出力されること

5. **設定反映の確認**
   - ConfigMapでモデル設定を変更（例: エイリアス追加）
   - OpenClawのAPI or Discord経由で変更が反映されていることを確認

6. **エラーケースの確認**
   - サイドカーが異常終了しないこと（`kubectl get pod`でRestartCountが増えないこと）
   - ConfigMapが空/不正な場合にOpenClawが安全にエラーハンドリングすること

## ロールバック計画

### 即座のロールバック（数分）

問題発生時はReloaderアノテーションを復元するだけで以前の動作に戻せる:

```bash
# 方法1: ArgoCD UIから以前のリビジョンにSync
# 方法2: Gitでrevert
git revert <commit-hash>
git push origin main
# ArgoCDのautoSyncで自動反映
```

### ロールバック手順の詳細

1. `deployment-openclaw.yaml` の `metadata.annotations` に以下を復元:
   ```yaml
   configmap.reloader.stakater.com/reload: "openclaw-config"
   ```
2. `containers` から `config-manager` サイドカーを削除
3. Git pushしてArgoCDの自動同期を待つ（または手動Sync）

### リスク評価

| リスク | 影響 | 確率 | 緩和策 |
|--------|------|------|--------|
| サイドカーがConfigMap変更を検知しない | 設定が反映されない | 低 | ログ監視、手動でPod再起動可能 |
| 原子的コピーが失敗 | 設定ファイルが破損 | 極低 | cp+mvパターン、OpenClawのパースエラーハンドリング |
| サイドカーのリソース不足 | OOMKill | 低 | 最小限のシェルスクリプトのため64MBで十分 |
| OpenClawのchokidarが変更を検知しない | 設定が反映されない | 極低 | PVC上のファイル直接書き換えは確実に検知される |

## 代替案の検討

### 検討した代替案

| 方式 | メリット | デメリット | 採用判断 |
|------|----------|------------|----------|
| **サイドカー方式（採用）** | シンプル、追加依存なし、OpenClawの組み込みhot-reloadを活用 | 最大2-3分の遅延 | 採用 |
| ConfigMap直接マウント+subPath | サイドカー不要 | シンボリックリンク問題で信頼性低 | 不採用 |
| inotifywait監視 | リアルタイム検知 | inotifywait未インストール、シンボリックリンク問題 | 不採用 |
| Webhook方式 | 即座に反映 | 追加インフラ必要、複雑 | 不採用 |
| Reloader + RollingUpdate | Reloader活用 | レプリカ1のため実質Recreateと同じ | 不採用 |
