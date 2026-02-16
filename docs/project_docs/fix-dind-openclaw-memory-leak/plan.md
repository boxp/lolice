# OpenClaw/DinD メモリリーク修正

## 問題

Grafanaモニタリングにより、openclaw Podの以下のコンテナでメモリ使用量が時間とともに増加し、タスク完了後も解放されない問題を確認。

| コンテナ | 現在のメモリ | 最大メモリ | 問題 |
|---------|------------|----------|------|
| dind | 1.29 GiB | 1.50 GiB | 階段状に増加、不可逆 |
| openclaw | 419 MiB | 1.41 GiB | スパイク後に部分回復のみ |
| config-manager | 1.09 MiB | 1.10 MiB | 正常 |

## 根本原因

### 原因1: dindのDockerリソース蓄積（主因）

- Dockerデーモンの設定（daemon.json）が未設定でデフォルト動作
- ログローテーションなし（json-fileドライバのデフォルト）
- タスク完了後の停止済みコンテナ、未使用イメージ、ビルドキャッシュが蓄積
- `/var/lib/docker`（emptyDir）のデータがPod再起動まで解放されない

### 原因2: openclawのNode.js heapサイズ未制限

- `--max-old-space-size`が未設定
- Node.jsがコンテナメモリ制限の75%（3 GiB）までheapを拡張可能
- GCが保守的で、確保したメモリをOSに即座に返却しない

## 解決策

### 1. dind daemon.json 設定追加

```json
{
  "storage-driver": "overlay2",
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "10m",
    "max-file": "3"
  }
}
```

ログファイルの無制限増加を防止。

### 2. docker-gc サイドカー追加

30分ごとに以下を実行する軽量サイドカーコンテナ：
- `docker container prune -f`: 停止済みコンテナの削除
- `docker image prune -f`: dangling イメージの削除
- `docker volume prune -f`: 未使用ボリュームの削除
- `docker builder prune -f --filter "until=1h"`: 1時間以上経過したビルドキャッシュの削除

リソース要件: CPU 10m/50m, メモリ 32Mi/64Mi（最小限）

### 3. NODE_OPTIONS 環境変数追加

```yaml
- name: NODE_OPTIONS
  value: "--max-old-space-size=2048"
```

Node.jsのheapを2 GiBに制限し、GCをより積極的に動作させる。

## 変更ファイル

- `argoproj/openclaw/deployment-openclaw.yaml`

## リスク評価

- **daemon.json**: dockerd起動前に書き込むため、起動順序の問題なし
- **docker-gc**: dind起動待ちロジックあり。pruneは未使用リソースのみ削除するため、実行中のタスクには影響なし
- **NODE_OPTIONS**: 2 GiBはopenclawの通常動作に十分。OOM時はKubernetesのrestart policyで自動復旧
