# BOXP-430 計画

- 非公開 HTML は既存 PVC に置いたまま読み取り専用 sidecar で一枚だけ配信し、公開 blob や新規認証情報を作らない。
- k8s namespace の既存 cloudflared tunnel を再利用し、専用 ClusterIP と Calico policy で到達元を限定する。
- 保護されない公開期間を避けるため、Owner-only Access を設定する boxp/arch を先に反映し、その後 lolice を反映する。
- 合成 fixture と実際の kustomize render を使う隔離 kind の Chainsaw で、応答、非公開パス、読み取り専用、ファイル欠損、通信制限を検証する。workflow の単体テストと kustomize build も実行する。
