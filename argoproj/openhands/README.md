# OpenHands on lolice Kubernetes

このディレクトリには、lolice Kubernetesクラスターで実行するためのOpenHandsの設定ファイルが含まれています。

## セットアップ手順

### 1. golyat-1ノードのIPアドレスを取得

ConfigMapに設定するgolyat-1ノードのIPアドレスを取得します:

```bash
kubectl get nodes golyat-1 -o jsonpath='{.status.addresses[?(@.type=="InternalIP")].address}'
```

### 2. ConfigMapを更新

取得したIPアドレスで`node-ip-config.yaml`を更新します:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: node-ip-config
  namespace: openhands
data:
  golyat-1-ip: "取得したIPアドレス"  # 例: "10.0.0.5"
```

### 3. マニフェストを適用

```bash
kubectl apply -f argoproj/openhands/
```

または、ArgoCD経由で自動的にデプロイされます。

## トラブルシューティング

### host.docker.internal解決エラー

OpenHandsはDockerコンテナ内から別のコンテナやホストに接続するために`host.docker.internal`というホスト名を使用します。Kubernetes環境では、これがgolyat-1ノードの実際のIPアドレスを指すように設定する必要があります。

この設定は以下の方法で行われています:

1. `node-ip-config` ConfigMapにgolyat-1ノードのIPアドレスを設定
2. initContainerによる`/etc/hosts`への追加設定
3. 環境変数`HOST_DOCKER_INTERNAL`による設定

これらの設定が正しく行われているか確認してください。

### Dockerリソースの管理

OpenHandsはDockerを使用して多数のコンテナを起動する可能性があるため、リソース管理が重要です。`docker-cleanup-cronjob.yaml`によって未使用のDockerリソースが毎日クリーンアップされます。

必要に応じてスケジュールを調整してください。

## 注意事項

- この設定はgolyat-1ノード上でのみOpenHandsを実行します
- Docker Socketを使用するため、セキュリティリスクが存在します
- リソース制限は適切に設定されていますが、必要に応じて調整してください 