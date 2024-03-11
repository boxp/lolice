# argoproj/external-secrets-operator

## apply aws access token

```
kubectl create secret generic awssm-secret \
  -n default \
  --from-literal=access-key=***** \
  --from-literal=secret-access-key='******'
```
