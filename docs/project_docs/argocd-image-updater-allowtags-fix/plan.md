# ArgoCD Image Updater allowTags エラー修正計画

## 問題
`argoproj/argocd-image-updater/imageupdaters/openclaw.yaml` の `allowTags` フィールドが配列として定義されているが、ArgoCD Image Updater v1.1.0 の CRD では `type: string` として定義されているため、バリデーションエラーが発生している。

### エラーメッセージ
```
spec.applicationRefs[0].images[0].commonUpdateSettings.allowTags: Invalid value: "array": ... must be of type string: "array"
```

## 原因
CRDの `allowTags` フィールドの説明:
```
AllowTags is a regex pattern for tags to allow.
type: string
```

現在のファイル（誤り）:
```yaml
allowTags:
  - matchRegex: "^\\d{12}$"
```

## 修正内容
`allowTags` を文字列に変更:
```yaml
allowTags: "^\\d{12}$"
```

## 影響範囲
- 変更対象ファイル: `argoproj/argocd-image-updater/imageupdaters/openclaw.yaml` のみ
- 他のImageUpdaterファイル（prod-hitohub, palserver等）は `allowTags` を使用していないため影響なし
