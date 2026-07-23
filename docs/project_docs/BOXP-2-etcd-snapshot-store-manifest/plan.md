# BOXP-2 etcd snapshot store manifest plan

## Context

The Kubernetes upgrade workflow stores etcd snapshots in a Longhorn-backed in-cluster volume. The persistent namespace, PVC, and storage pod should be managed by lolice GitOps manifests rather than created imperatively from the arch Ansible role.

## Plan

- Add a new Argo CD application at `argoproj/etcd-snapshot-store`.
- Manage namespace `etcd-snapshots`, PVC `etcd-snapshots`, and a single-replica storage Deployment.
- Use the existing `longhorn` StorageClass with a 10Gi `ReadWriteOnce` volume.
- Label the Deployment pods with `app.kubernetes.io/name=etcd-snapshot-store` so arch can find the ready pod by selector.
- Add the application to the root `argoproj/kustomization.yaml`.

## Validation

- Build the new kustomize application.
- Build the root `argoproj` kustomization.
