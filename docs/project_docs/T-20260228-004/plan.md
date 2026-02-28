# T-20260228-004: tailscale-operator PROXY_TAGS 型不整合修正

## Root Cause

ArgoCD sync failure:
```
failed to create typed patch object ... env[name="PROXY_TAGS"].value: expected string, got list
```

In `argoproj/tailscale-operator/helm/values.yaml`, `proxyConfig.defaultTags` was defined as a YAML list:
```yaml
proxyConfig:
  defaultTags:
    - "tag:k8s-operator"
```

However, the Helm chart template (`deployment.yaml` L78-79) uses it directly without `join`:
```yaml
- name: PROXY_TAGS
  value: {{ .Values.proxyConfig.defaultTags }}
```

This differs from `operatorConfig.defaultTags` (L60) which uses `join`:
```yaml
- name: OPERATOR_INITIAL_TAGS
  value: {{ join "," .Values.operatorConfig.defaultTags }}
```

The chart's own default `values.yaml` defines `proxyConfig.defaultTags` as a string (`"tag:k8s"`),
confirming the expected type is string, not list.

## Fix

Change `proxyConfig.defaultTags` from list to string:
```yaml
proxyConfig:
  defaultTags: "tag:k8s-operator"
```

## Validation

- Chart template analysis confirms `{{ .Values.proxyConfig.defaultTags }}` requires string type
- Chart default values.yaml confirms `defaultTags: "tag:k8s"` (string)
- Modified values.yaml passes YAML parsing as string type

## Risk / Rollback

- **Risk**: Minimal - single value change from list to string, matching chart's expected type
- **Rollback**: Revert the single line change in values.yaml
