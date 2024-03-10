# k8s/calico

[Quickstart](https://docs.tigera.io/calico/latest/getting-started/kubernetes/quickstart)を実行しただけなので manifest は今のところなし

https://github.com/projectcalico/calico/issues/8407 の問題で v3.27.3 リリースまでは arm クラスターで動作しないので、v2.26.4 を使っている

```
kubectl create -f https://raw.githubusercontent.com/projectcalico/calico/v3.26.4/manifests/tigera-operator.yaml
kubectl create -f https://raw.githubusercontent.com/projectcalico/calico/v3.26.4/manifests/custom-resources.yaml
```
