# k8s-ecr-token-updater

## default のサービスアカウントで ecr からの pull を可能にする patch(実行済み)

```
kubectl patch serviceaccount default -p '{"imagePullSecrets": [{"name": "regcred"}]}' -n <namespace>
```

## cronjob の手動実行

```
kubectl create job --from=cronjob/ecr-registry-helper ecr-registry-helper-manual -n k8s-ecr-token-updater
```
