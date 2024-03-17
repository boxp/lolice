# k8s-ecr-token-updater

## default のサービスアカウントで ecr からの pull を可能にする patch(実行済み)

```
kubectl patch serviceaccount default -p '{"im
agePullSecrets": [{"name": "regcred"}]}' -n k8s-ecr-token-update
r
```
