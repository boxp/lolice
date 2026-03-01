# T-20260227-016: Tailscale Kubernetes Operator PoC

> **再ベースライン: 2026-03-01**
> Operator導入（Phase 1）が完了し、2件のインシデント修正を経て安定稼働中。
> 本計画書を現状に合わせて再ベースラインし、PoCゴールを「argocd-diff の tailscale 経由 keyless 認証化」に再定義する。

## 概要

IPハードコード（TS_ROUTES）依存を避けるため、Tailscale Kubernetes Operator を使った「tailnet 内プライベート公開」方式の PoC を実施する。最終的な PoC 完了条件は、GitHub Actions の `argocd-diff` ワークフローを Tailscale 経由の keyless 認証で動作させることである。

> **重要**: 本 PoC で公開されるサービスは **tailnet 限定** であり、パブリックインターネットには公開されません。tailnet に参加しているデバイスからのみアクセス可能です。

## PoC 完了条件

以下を全て満たした時点で PoC 完了とする:

1. GitHub Actions の `argocd-diff` ワークフローが、Tailscale WIF（Workload Identity Federation）経由で ArgoCD API にアクセスし、`argocd app diff` を正常に実行できること
2. Cloudflare Service Token（長寿命キー）に依存せず、OIDC ベースの keyless 認証であること
3. 既存 Cloudflare 経路が影響を受けず、フォールバックとして利用可能なこと
4. エフェメラルノードがジョブ完了後に自動削除されること

## 背景

### 既存方式（Subnet Router / PR #493）の課題

PR #493 で検討された subnet router 方式では：
- `TS_ROUTES` に Service の ClusterIP を `/32` で指定する必要がある
- ClusterIP はデプロイ後に動的に決まるため、2段階デプロイが必要
- サービス追加のたびに `TS_ROUTES` の更新が必要
- IP ベースの経路管理は運用負荷が高い

→ Operator 方式に方針転換（PR #493 は open のまま未マージ）。

### Operator 方式のアプローチ

Tailscale Kubernetes Operator は：
- Service に `tailscale.com/expose: "true"` Annotation を付与するだけで tailnet に公開
- 各サービスが tailnet 上で一意のホスト名を持つ（DNS ベース）
- IP ハードコード不要
- サービス追加は Annotation のみで完結

### 初期計画との関係

- 初期計画: [T-20260226-902/plan.md](../T-20260226-902/plan.md)（案A: GitHub Actions WIF + subnet router 推奨）
- 本計画は初期計画の「案B: K8s Operator」を採用し、段階的に WIF keyless 認証まで進める方針

## 設計

### アーキテクチャ

```
tailnet デバイス / GitHub Actions (WIF)
  ↓ WireGuard (tailnet 限定)
Tailscale Operator (proxy Pod 自動生成)
  ↓ ClusterIP
対象 Service (e.g. argocd-server)
```

### ディレクトリ構成

```
argoproj/tailscale-operator/
├── application.yaml       # ArgoCD Application (Helm + Kustomize multi-source)
├── helm/
│   └── values.yaml        # Helm chart カスタム values
├── namespace.yaml          # tailscale-operator namespace
├── external-secret.yaml   # OAuth credentials (SSM → K8s Secret)
├── networkpolicy.yaml     # Calico NetworkPolicy
└── kustomization.yaml     # Kustomize 設定
```

### コンポーネント詳細

#### 1. Helm Chart

- Chart: `tailscale-operator` from `https://pkgs.tailscale.com/helmcharts`
- Version: `1.80.3`
- OAuth credentials は ExternalSecret で管理

#### 2. ExternalSecret

- SSM パラメータ:
  - `/lolice/tailscale/operator-oauth-client-id`
  - `/lolice/tailscale/operator-oauth-client-secret`
- K8s Secret: `tailscale-operator-oauth`
- **前提**: `boxp/arch` 側で Tailscale OAuth Client の Terraform リソース作成済み（arch PR #7268）

#### 3. NetworkPolicy (Calico v3)

- Egress:
  - DNS (kube-system/kube-dns:53)
  - Kubernetes API (10.96.0.1/32:443) — リソース監視
  - Tailscale coordination server (443/TCP)
  - DERP relay (3478/UDP)
  - 対象 Service への通信（namespace 横断）
- Ingress:
  - Webhook (443/TCP) — K8s API Server からの Admission Webhook

#### 4. ArgoCD Application

- Multi-source pattern (grafana-alloy と同等):
  - Source 1: lolice リポジトリ参照 (`ref: lolice`)
  - Source 2: Helm chart (`tailscale-operator`)
  - Source 3: Kustomize パッチ（namespace, external-secret, networkpolicy）
- `ServerSideApply=true` (CRD 含むため)

## 移行計画（フェーズ別）

### Phase 1: Operator 導入 — **完了**

Tailscale Kubernetes Operator のインストール。既存 Cloudflare 経路は変更なし。

| 項目 | 状態 | PR |
|------|------|-----|
| Operator Helm chart デプロイ | 完了 | [#494](https://github.com/boxp/lolice/pull/494) |
| ExternalSecret (OAuth credentials) | 完了 | #494 |
| Calico NetworkPolicy | 完了 | #494 |
| ArgoCD Application (multi-source) | 完了 | #494 |
| PROXY_TAGS 型不整合修正 | 完了 | [#497](https://github.com/boxp/lolice/pull/497) |
| OAuth Secret 名不一致修正 | 完了 | [#500](https://github.com/boxp/lolice/pull/500) |

### Phase 2: ArgoCD Service の tailnet 公開 — 未着手

ArgoCD Service に Annotation を追加し、tailnet 経由でのアクセスを確認する。

**タスク:**

1. ArgoCD Server Service に `tailscale.com/expose: "true"` と `tailscale.com/hostname: "lolice-argocd"` Annotation を追加
2. Operator が Proxy Pod を自動生成することを確認
3. tailnet 参加済みデバイスから `lolice-argocd` ホスト名で ArgoCD UI/API にアクセスできることを確認
4. Tailscale ACL で ArgoCD へのアクセスを適切に制限

**承認ポイント:**
- Annotation 追加のPRをマージする前に、tailnet 上のホスト名・ACL 設定をレビュー
- Proxy Pod のリソース消費が許容範囲内であることを確認

**前提条件:**
- `boxp/arch` 側で Tailscale ACL に ArgoCD 向けルールを追加（`tag:k8s-operator` デバイスへのアクセス制御）

### Phase 3: argocd-diff の Tailscale WIF keyless 化 — 未着手（本丸）

GitHub Actions の `argocd-diff` ワークフローを Tailscale WIF 経由に切り替え、Cloudflare Service Token 依存を排除する。

**タスク:**

1. `boxp/arch` に Tailscale Trust Credential（WIF）を Terraform で追加
   - Issuer: `https://token.actions.githubusercontent.com`
   - Subject: `repo:boxp/lolice:pull_request`
   - Tags: `tag:ci`
2. テスト用 GitHub Actions ワークフロー `.github/workflows/test-tailscale-wif.yaml` を作成し、WIF 接続を検証
3. `argocd-diff.yaml` に Tailscale 接続パスを追加（Cloudflare パスと並行動作）
4. 実際の PR で両経路（Cloudflare / Tailscale）の動作を比較検証
5. Tailscale 経路のみで安定動作することを確認後、Cloudflare パスを削除

**承認ポイント:**
- WIF Trust Credential の Terraform PR（`boxp/arch`）レビュー・承認
- テストワークフロー実行結果のレビュー
- `argocd-diff.yaml` の本番切替前レビュー

**前提条件:**
- Phase 2 完了（ArgoCD が tailnet 上で到達可能）
- `boxp/arch` で WIF Trust Credential が Terraform 管理されていること
- GitHub リポジトリで `id-token: write` パーミッションが利用可能

**成功基準:**
- `argocd app diff` が Tailscale WIF 経路で正常実行される
- エフェメラルノードがジョブ完了後に自動削除される
- GitHub Secrets に新たな長寿命シークレットが不要
- 既存 Cloudflare 経路が影響を受けない

### Phase 4: Cloudflare 経路の段階的撤去 — 未着手

Phase 3 の安定稼働を十分に確認した後、Cloudflare 経路を段階的に撤去する。

**タスク:**

1. Cloudflare ArgoCD API 用 Tunnel の停止
2. 関連する GitHub Secrets（`ARGOCD_API_TOKEN_ID`, `ARGOCD_API_TOKEN_SECRET`）の削除
3. Cloudflare Access Service Token の失効

**承認ポイント:**
- Phase 3 で 2 週間以上の安定稼働を確認した後にのみ実施
- ロールバック手順を事前に確認

> **注意:** Phase 4 は PoC 完了条件には含まれない。Phase 3 完了をもって PoC 完了とする。

## 既知インシデントと再発防止

### インシデント 1: PROXY_TAGS 型不整合（T-20260228-004）

**事象:** ArgoCD sync failure — `env[name="PROXY_TAGS"].value: expected string, got list`

**原因:** `helm/values.yaml` で `proxyConfig.defaultTags` を YAML リスト形式で定義していたが、Helm chart テンプレートは文字列型を期待していた。

```yaml
# NG: リスト形式
proxyConfig:
  defaultTags:
    - "tag:k8s-operator"

# OK: 文字列形式
proxyConfig:
  defaultTags: "tag:k8s-operator"
```

**修正:** PR [#497](https://github.com/boxp/lolice/pull/497) でリストから文字列に修正。

**再発防止:**
- Helm chart の `values.yaml` デフォルト値とテンプレートの型を事前に確認する
- 特に `{{ .Values.xxx }}` で直接展開される値は、chart のデフォルト `values.yaml` の型に合わせる
- `{{ join "," .Values.xxx }}` のような変換がある場合のみリスト形式を使用する

### インシデント 2: OAuth Secret 名不一致（T-20260301-001）

**事象:** `MountVolume.SetUp failed ... secret "operator-oauth" not found`

**原因:** Helm chart (v1.80.3) のデプロイメントテンプレートは、`oauthSecretVolume` 未設定時に `secretName: operator-oauth` をハードコードする。一方、lolice の ExternalSecret は `tailscale-operator-oauth` という名前で Secret を作成しており、名前が不一致。また `operatorConfig.existingSecret` は chart に存在しない values であり、設定しても効果がなかった。

**修正:** PR [#500](https://github.com/boxp/lolice/pull/500) で `oauthSecretVolume` を明示的に設定し、ExternalSecret が作成する Secret 名と一致させた。

```yaml
oauthSecretVolume:
  secret:
    secretName: tailscale-operator-oauth
```

**再発防止:**
- Helm chart のテンプレートソースを直接確認し、実際のデフォルト値とフォールバック挙動を把握する
- `values.yaml` に存在しない（undocumented な）values key を使用しない
- ExternalSecret のターゲット Secret 名と Helm chart が参照する Secret 名の一致を PR レビュー時にチェックする

## 公開範囲について

**本 PoC で公開されるサービスは tailnet 限定です。**

- Tailscale Kubernetes Operator は、対象サービスを **tailnet 内のデバイスからのみ** アクセス可能にします
- パブリックインターネットには一切公開されません
- アクセスには tailnet への参加（Tailscale クライアントのインストールと認証）が必要です
- Tailscale ACL により、さらに細かいアクセス制御が可能です

## 既存方式との比較

| 項目 | Subnet Router (PR #493) | Operator (本 PoC) |
|------|------------------------|-------------------|
| IP ハードコード | 必要 (TS_ROUTES に ClusterIP/32) | 不要 |
| サービス追加 | TS_ROUTES 更新 + 再デプロイ | Annotation 追加のみ |
| DNS 名 | なし (IP 直指定) | あり (tailnet hostname) |
| デプロイ手順 | 2段階 (IP 確認後に再デプロイ) | 1段階 |
| CRD | なし | あり (Operator が管理) |
| リソース消費 | 低 (単一 Pod) | 中 (Operator + Service 毎に Proxy Pod) |
| K8s 権限 | 最小 (Secret のみ) | 広い (ClusterRole: Services, Endpoints 等) |
| Helm 依存 | なし | あり |
| 成熟度 | 安定 (シンプル) | Tailscale 公式サポート |
| tailnet 公開 | ルーティング経由 | Service 単位で個別公開 |

## ロールバック手順

1. `argoproj/kustomization.yaml` から `tailscale-operator/application.yaml` を削除してコミット
2. ArgoCD が自動 sync → Application 削除 → 全関連リソース cascade 削除
3. CRD はクラスターに残るため、必要に応じて手動削除:
   ```bash
   kubectl delete crd connectors.tailscale.com proxyclasses.tailscale.com dnsconfigs.tailscale.com proxygroups.tailscale.com
   ```
4. 代替（即時）: `argocd app delete tailscale-operator --cascade`

## 前提条件（`boxp/arch` 側で必要な作業）

**arch PR**: https://github.com/boxp/arch/pull/7268 (T-20260227-017)

- Tailscale OAuth Client 用 SSM パラメータの Terraform 管理化（`terraform/tailscale/lolice/oauth.tf`）
- OAuth Client ID / Secret を SSM に保存:
  - `/lolice/tailscale/operator-oauth-client-id`
  - `/lolice/tailscale/operator-oauth-client-secret`
- Tailscale ACL に `tag:k8s-operator` の権限設定

### 運用手順

arch PR #7268 により、SSM パラメータは Terraform で管理されます。
初回のみ以下の手動ステップが必要ですが、以降は ExternalSecret による自動同期で運用できます：

1. arch PR マージ後、Terraform apply で SSM パラメータが作成される
2. Tailscale admin console で OAuth client を作成（Settings > OAuth clients）
   - Scopes: `auth_keys`, `devices` (Write)
   - Tag: `tag:k8s-operator`
3. 取得した credentials を SSM に投入:
   ```bash
   aws ssm put-parameter --name "/lolice/tailscale/operator-oauth-client-id" \
       --value "<CLIENT_ID>" --type SecureString --overwrite
   aws ssm put-parameter --name "/lolice/tailscale/operator-oauth-client-secret" \
       --value "<CLIENT_SECRET>" --type SecureString --overwrite
   ```
4. 本 PR マージ後、ExternalSecret が 1h 間隔で SSM から自動同期

## 関連ドキュメント

| チケット | 内容 | 状態 |
|----------|------|------|
| [T-20260226-902](../T-20260226-902/plan.md) | Tailscale WIF PoC 初期計画（案A/B/C比較） | 完了（計画策定） |
| [T-20260228-004](../T-20260228-004/plan.md) | PROXY_TAGS 型不整合修正 | 完了（PR #497） |
| [T-20260301-001](../T-20260301-001/plan.md) | OAuth Secret 名不一致修正 | 完了（PR #500） |

## PR 履歴

| PR | タイトル | 状態 | マージ日 |
|----|---------|------|----------|
| [#490](https://github.com/boxp/lolice/pull/490) | docs: lolice向け Tailscale Workload Identity PoC導入計画 | Merged | 2026-02-27 |
| [#493](https://github.com/boxp/lolice/pull/493) | feat(tailscale): add subnet router K8s manifests for PoC | Open | — |
| [#494](https://github.com/boxp/lolice/pull/494) | feat(tailscale): add Kubernetes Operator PoC for tailnet-only exposure | Merged | 2026-02-28 |
| [#497](https://github.com/boxp/lolice/pull/497) | fix: normalize tailscale-operator PROXY_TAGS env type for ArgoCD sync | Merged | 2026-02-28 |
| [#500](https://github.com/boxp/lolice/pull/500) | fix: wire tailscale operator oauthSecretVolume to tailscale-operator-oauth | Merged | 2026-03-01 |
