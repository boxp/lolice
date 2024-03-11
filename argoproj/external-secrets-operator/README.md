# argoproj/external-secrets-operator

## apply aws access token

```
kubectl create secret generic awssm-secret \
  -n default \
  --from-literal=access-key=***** \
  --from-literal=secret-access-key='******'
```

## add application

```
argocd app create external-secrets-operator --repo https://github.com/boxp/lolice --path argoproj/external-secrets-operator --dest-server https://kubernetes.default.svc --dest-namespace external-secrets
```