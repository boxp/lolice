# Argo CD Image Updater spec.namespace removal plan

## Background

Argo CD keeps showing drift for `argocd-image-updater.argoproj.io/ImageUpdater` resources because the manifests include `spec.namespace: argocd`.

`namespace` is required under `metadata` to place the custom resources in the `argocd` namespace, but the `ImageUpdater` spec does not need this property.

## Scope

- Remove `spec.namespace` from all ImageUpdater manifests under `argoproj/argocd-image-updater/imageupdaters/`.
- Keep `metadata.namespace: argocd` unchanged.
- Do not change update strategies, image targets, or write-back configuration.

## Validation

- Confirm no ImageUpdater manifest still contains `spec.namespace`.
- Run `kubectl kustomize argoproj/argocd-image-updater/imageupdaters` to ensure manifests still render.

## Expected result

Argo CD stops reporting the repeated diff that removes `spec.namespace` from ImageUpdater resources.
