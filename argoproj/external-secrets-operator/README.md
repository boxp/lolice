# argoproj/external-secrets-operator

external-secrets は Helm Repository(https://charts.external-secrets.io) を ArgoCD から sync してます
ここにおいてあるのは external-secrets 以外の manifestのみなのであしからず

## apply aws access token

```
kubectl create secret generic awssm-secret \
  -n default \
  --from-literal=access-key=***** \
  --from-literal=secret-access-key='******'
```
