# T-20260301-001: tailscale-operator oauth secret 名不一致修正

## Root Cause

tailscale-operator Helm chart (v1.80.3) のデプロイメントテンプレートは、
`oauthSecretVolume` が未設定の場合 `secretName: operator-oauth` をハードコードする。

一方、lolice の ExternalSecret は `tailscale-operator-oauth` という名前で
Secret を作成しているため、名前の不一致により Pod が起動できない。

```
MountVolume.SetUp failed ... secret "operator-oauth" not found
```

加えて、`operatorConfig.existingSecret` は chart に存在しない values であり、
設定しても何の効果もなかった。

## Fix

`argoproj/tailscale-operator/helm/values.yaml` を以下のように修正:

1. `oauth: {}` に変更（空の clientId/clientSecret を設定しない）
2. 存在しない `operatorConfig.existingSecret` を削除
3. `oauthSecretVolume` を追加し、ExternalSecret が作成する
   `tailscale-operator-oauth` を参照

## Chart Template Reference

```yaml
# templates/deployment.yaml (抜粋)
volumes:
  - name: oauth
    {{- with .Values.oauthSecretVolume }}
    {{- toYaml . | nindent 10 }}
    {{- else }}
    secret:
      secretName: operator-oauth
    {{- end }}
```

## Validation

- ExternalSecret target name: `tailscale-operator-oauth` ✓
- oauthSecretVolume.secret.secretName: `tailscale-operator-oauth` ✓
- Secret keys (`client_id`, `client_secret`) are consistent ✓
