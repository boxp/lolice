# OpenClaw Config ホットリロード機能の実装計画

## 目的

OpenClawのConfigMap更新時にPodが再生成（Recreate）されるのを抑制し、OpenClawが内蔵するchokidarベースのファイル監視によるホットリロード機能を活用する。これにより、設定変更時のダウンタイムをゼロにする。

## 背景

- 現在、Stakater ReloaderのアノテーションによりConfigMap変更時にPodが再生成される
- OpenClawはchokidar（300msデバウンス）でファイル変更を自動検知し、ホットリロードする機能を持っている
- ConfigMapのマウントはKubernetesがシンボリックリンクで管理するため、chokidarが直接検知できない場合がある
- サイドカーでConfigMap→PVC間のコピーを行うことで、確実にホットリロードを実現する

## 反映フロー

```
Git push → ArgoCD sync → ConfigMap更新
  → config-manager-sidecar検知(~60秒)
    → PVCにコピー
      → chokidar検知(~300ms)
        → ホットリロード
          → ダウンタイムなし
```

## 変更内容

### 1. `deployment-openclaw.yaml` の修正

- **Reloaderアノテーション無効化**: `configmap.reloader.stakater.com/reload` を `"false"` に変更
- **config-manager-sidecar追加**: ConfigMap変更を定期的にチェックし、変更があればPVCにコピーするサイドカーコンテナを追加

### 2. `configmap-openclaw-cron.yaml` の新規作成

- OpenClawのcronジョブ設定をGitOpsで管理するためのConfigMap
- `crontab.json` にcronジョブ定義を格納

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
      # ConfigMapの変更を検知してPVCにコピー
      LAST_HASH=""
      while true; do
        CURRENT_HASH=$(md5sum /config/openclaw.json | cut -d' ' -f1)
        if [ "$CURRENT_HASH" != "$LAST_HASH" ]; then
          cp /config/openclaw.json /home/node/.openclaw/openclaw.json
          LAST_HASH="$CURRENT_HASH"
        fi
        sleep 60
      done
```

### セキュリティ

- サイドカーはメインコンテナと同等のsecurityContext（非特権、runAsUser: 1000）
- ConfigMapボリュームはreadOnlyでマウント
- リソース制限は最小限（CPU: 50m, Memory: 64Mi）
