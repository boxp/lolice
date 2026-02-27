# Tailscale Workload Identity Federation PoC 導入計画

> **要約**: lolice クラスターの GitHub Actions CI/CD パイプラインで利用している Cloudflare Service Token（長寿命キー）を、Tailscale Workload Identity Federation（2026-02-19 GA）に置き換える PoC 計画。推奨案は「GitHub Actions WIF + 限定 subnet router による ArgoCD diff のキーレス化」で、2 週間以内に検証可能。GitHub Actions 側は WIF で完全キーレス化し、クラスター側は subnet router（auth key を既存 External Secrets パターンで管理）で ArgoCD API への到達性を確保する。既存 Cloudflare 経路は維持したまま並行導入し、失敗時は即座にロールバック可能な設計とする。

---

## 目次

1. [現状整理](#1-現状整理)
2. [PoC 候補の比較](#2-poc-候補の比較)
3. [推奨 PoC プラン](#3-推奨-poc-プラン)
4. [実行計画](#4-実行計画)
5. [セキュリティ境界の整理](#5-セキュリティ境界の整理)

---

## 1. 現状整理

### 1.1 公開経路

lolice クラスターでは、全ての外部公開サービスに Cloudflare Tunnel（cloudflared）を使用している。

| サービス | Tunnel 配置 | プロトコル | 備考 |
|---|---|---|---|
| ArgoCD UI | 専用 Deployment | HTTP/2 | `argocd-tunnel-token` |
| ArgoCD API | 専用 Deployment | gRPC-web | `argocd-api-tunnel-token`（CI 用） |
| Grafana | LoadBalancer 併用 | HTTP | `prometheus-operator-tunnel-token` |
| K8s Dashboard | 専用 Deployment | HTTP | `kubernetes-dashboard-tunnel-token` |
| Bastion SSH | sidecar | SSH (2222) | `bastion-tunnel-token` |
| OpenClaw | sidecar | HTTP | `/lolice/openclaw/tunnel-token` |
| Longhorn UI | 専用 Deployment | HTTP | `longhorn-tunnel-token` |
| HitoHub (prod/stage) | NodePort 併用 | HTTP | 各環境用 tunnel-token |

全トンネルトークンは AWS SSM Parameter Store → External Secrets Operator 経由で K8s Secret に同期される（更新間隔: 1 時間）。

### 1.2 認証経路（課題の中心）

**GitHub Actions → ArgoCD API アクセスフロー:**

```
GitHub Actions Runner
  ↓ CF-Access-Client-Id / CF-Access-Client-Secret ヘッダー付与
Cloudflare Access (Service Token 検証)
  ↓
Cloudflare Tunnel (argocd-api 専用)
  ↓
ArgoCD Server (gRPC-web)
  ↓ ARGOCD_AUTH_TOKEN で認証
argocd app diff 結果を PR コメントに投稿
```

**課題:**
- `ARGOCD_API_TOKEN_ID` / `ARGOCD_API_TOKEN_SECRET`: Cloudflare Access Service Token（長寿命、手動ローテーション）
- `ARGOCD_AUTH_TOKEN`: ArgoCD API トークン（長寿命）
- これらが GitHub Secrets に静的に保存されており、漏洩時の影響が大きい
- ローテーション運用が手動で、失効管理が属人的

### 1.3 既存の Tailscale 利用状況

- OpenClaw の gateway 設定で `gateway.tailscale` の項目が存在するが、現時点では未実装
- クラスター全体として Tailscale の本格導入は未着手

---

## 2. PoC 候補の比較

### 案 A: GitHub Actions WIF + subnet router（ArgoCD diff 限定置換）

GitHub Actions の OIDC トークンを利用し、Tailscale Workload Identity Federation 経由で tailnet にエフェメラルノードとして参加。K8s クラスター上の subnet router Pod を経由して ArgoCD API へアクセスする。**スコープは ArgoCD diff の CI/CD パイプラインに限定**し、他のサービスの公開経路は変更しない。

**変更範囲:**
- `.github/workflows/argocd-diff.yaml` の修正
- Tailscale 管理コンソールで Trust Credential 追加
- K8s クラスターに Tailscale subnet router Pod をデプロイ（ArgoCD Service ClusterIP への経路のみアドバタイズ）
- Tailscale ACL でアクセス制御ルール定義

**案 C との違い:** 案 A は ArgoCD API への到達性のみを確保する最小構成。subnet router のアドバタイズ範囲を ArgoCD Service の ClusterIP に限定する。案 C は内部ネットワーク全体（192.168.10.0/24）を広告するハイブリッド構成。

### 案 B: K8s Operator + Workload Identity Federation（クラスター全体統合）

Tailscale Kubernetes Operator を導入し、クラスター上のサービスを tailnet に直接公開。Operator 自身の認証にも Workload Identity Federation を利用する。

**変更範囲:**
- Tailscale K8s Operator のデプロイ（ArgoCD Application 追加）
- 各サービスの Service/Ingress を Tailscale Operator 経由に変更
- Operator 用 OIDC Issuer 設定（K8s クラスターの OIDC エンドポイントが公開アクセス可能であること）
- 全既存 cloudflared Deployment の段階的置換

### 案 C: Subnet Router + GitHub Actions WIF（ハイブリッド）

クラスターの 1 ノードに Tailscale subnet router を配置し、内部ネットワーク（192.168.10.0/24）を tailnet に広告。GitHub Actions は WIF で tailnet に接続し、subnet router 経由で内部サービスにアクセスする。

**変更範囲:**
- 1 台のノードに Tailscale をインストール（または DaemonSet で subnet router Pod をデプロイ）
- Tailscale 管理コンソールで subnet routes を承認
- GitHub Actions ワークフローの修正
- Tailscale ACL 設定

### 比較評価表

| 評価軸 | 案 A: GHA WIF + subnet router（限定） | 案 B: K8s Operator 全体統合 | 案 C: Subnet Router + WIF（広域） |
|---|---|---|---|
| **効果** | 高: CI/CD の長寿命キー依存を直接解消 | 最高: 全サービスの認証基盤を統一 | 中: CI/CD キーレス化 + 広域内部到達性 |
| **実装難易度** | 低〜中: ワークフロー修正 + 限定 subnet router + ACL | 高: 全サービスの移行設計が必要 | 低: subnet router 1 台（広域ルート） |
| **運用負荷** | 低: エフェメラルノードは自動削除 | 中: Operator のバージョン管理が追加 | 低〜中: subnet router の可用性管理 |
| **リスク** | 低: 既存経路を維持したまま並行検証可能 | 高: Operator は beta、OIDC 公開要件あり | 低: subnet router 停止時は既存経路にフォールバック |
| **ロールバック容易性** | 最高: ワークフローを元に戻すだけ | 低: 移行済みサービスの巻き戻しが複雑 | 高: subnet router を停止するだけ |
| **2 週間で検証可能か** | 可能 | 困難（設計だけで 2 週間以上） | 可能 |
| **将来の拡張性** | 中: 案 B/C への段階的拡張が可能 | 最高: 最終目標に最も近い | 高: 案 A と組み合わせて段階拡張可能 |

### 推奨: 案 A（GitHub Actions WIF + subnet router 限定構成）

**選定理由:**
1. 課題の核心（CI/CD の長寿命 Service Token 依存）を最小の変更で解消できる
2. 既存 Cloudflare 経路を一切変更せず、並行運用で安全に検証可能
3. Workload Identity Federation の GA 機能のみを使用し、beta 機能への依存がない
4. 検証結果を元に、将来的に案 B/C への段階的拡張が可能

---

## 3. 推奨 PoC プラン

### 3.1 ゴール

GitHub Actions の `argocd-diff` ワークフローで、Cloudflare Service Token を使わず Tailscale Workload Identity Federation 経由で ArgoCD API にアクセスし、`argocd app diff` を正常に実行できることを検証する。

### 3.2 ステップ分解（2 週間）

#### Week 1: 基盤構築

| Day | タスク | 成果物 |
|---|---|---|
| 1-2 | Tailscale アカウント設定・tailnet 作成（既存がない場合）、ACL の初期設計 | tailnet 環境、ACL ポリシーファイル |
| 2-3 | K8s クラスターに Tailscale subnet router Pod をデプロイ（DaemonSet or 単体 Deployment） | subnet router マニフェスト、ArgoCD Application |
| 3-4 | Tailscale 管理コンソールで subnet routes 承認、ACL でアクセス範囲を制限 | 承認済み subnet routes、ACL ルール |
| 4-5 | Tailscale 管理コンソールで GitHub Actions 用 Trust Credential を作成 | Client ID、Audience 値 |

#### Week 2: 検証・統合

| Day | タスク | 成果物 |
|---|---|---|
| 6-7 | テスト用 GitHub Actions ワークフロー作成（tailnet 接続 → ArgoCD API 疎通確認） | `.github/workflows/test-tailscale-wif.yaml` |
| 8-9 | `argocd-diff.yaml` に Tailscale 接続パスを追加（Cloudflare パスと並行） | 更新済みワークフロー |
| 10 | 実際の PR で両経路（Cloudflare / Tailscale）の動作を比較検証 | 検証結果レポート |
| 11-12 | 結果レビュー、成功基準の判定、次フェーズ計画の策定 | 最終レポート |

### 3.3 成功基準（KPI / 受け入れ条件）

| # | 基準 | 測定方法 |
|---|---|---|
| 1 | GitHub Actions ランナーが WIF 経由で tailnet に接続できる | ワークフローログで `tailscale status` が正常出力 |
| 2 | tailnet 内部経路で ArgoCD API に到達できる | `curl` で ArgoCD API エンドポイントの応答を確認 |
| 3 | `argocd app diff` が Tailscale 経路で正常に実行される | 既存 Cloudflare 経路と同一の diff 結果が得られる |
| 4 | エフェメラルノードがジョブ終了後に自動削除される | Tailscale 管理コンソールでノードが消えていることを確認 |
| 5 | GitHub Secrets に新たな長寿命シークレットが不要である | Client ID と Audience のみ（非秘密情報）が使用されている |
| 6 | 既存 Cloudflare 経路が影響を受けない | Cloudflare 経路での `argocd app diff` が引き続き正常動作 |

### 3.4 セキュリティ境界

**ネットワーク層:**
- Tailscale（WireGuard）: GitHub Actions ランナー ↔ subnet router 間の暗号化トンネル
- subnet router: tailnet から K8s 内部ネットワーク（ArgoCD Service）へのブリッジ
- Calico NetworkPolicy: K8s 内部の Pod 間通信制御（変更なし）

**ID 層:**
- GitHub Actions OIDC: ランナーの身元証明（リポジトリ・ブランチ・ワークフロー）
- Tailscale WIF: OIDC トークン → 短寿命 tailnet アクセストークンへの交換
- Tailscale ACL: タグベースのアクセス制御（`tag:ci` → ArgoCD API ポートのみ許可）
- ArgoCD Auth Token: API 操作の認可（既存のまま維持、将来的に OIDC 化を検討）

**責務分離:**

| レイヤー | 責務 | 技術 |
|---|---|---|
| ネットワーク到達性 | tailnet 内部でのみ ArgoCD API に到達可能にする | Tailscale WireGuard + ACL |
| 認証（Authentication） | 接続元ワークロードの身元証明 | GitHub OIDC → Tailscale WIF |
| 認可（Authorization） | API 操作の権限制御 | ArgoCD RBAC + Auth Token |
| 暗号化 | 通信経路の保護 | WireGuard（ランナー〜subnet router 間）+ TLS（ArgoCD API） |

---

## 4. 実行計画

### 4.1 前提条件

| # | 前提 | 確認方法 |
|---|---|---|
| 1 | Tailscale アカウントが利用可能（Free/Personal/Enterprise いずれか） | 管理コンソールにログイン |
| 2 | GitHub リポジトリ (`boxp/lolice`) で OIDC トークン発行が可能 | `permissions: id-token: write` がブロックされていないこと |
| 3 | K8s クラスターに新規 Pod をデプロイ可能 | ArgoCD で Application を追加できること |
| 4 | Tailscale 管理コンソールへの管理者アクセスがある | Trust Credential・ACL の編集権限 |

### 4.2 GitHub 設定

1. **リポジトリ Secrets/Variables に追加（WIF 用）:**
   - `TS_OAUTH_CLIENT_ID`: Tailscale Trust Credential の Client ID（非秘密情報だが Variables でも可）
   - `TS_AUDIENCE`: Tailscale Trust Credential の Audience 値

2. **ワークフロー権限:**
   - `id-token: write` パーミッションの追加（OIDC トークン要求に必要）

3. **既存 Secrets は削除しない:**
   - `ARGOCD_API_TOKEN_ID` / `ARGOCD_API_TOKEN_SECRET`: Cloudflare 経路のフォールバック用に保持

### 4.3 Tailscale 設定

1. **Trust Credential 作成:**
   - Issuer: GitHub Actions (`https://token.actions.githubusercontent.com`)
   - Subject: `repo:boxp/lolice:pull_request`（`pull_request` トリガーでの OIDC Subject に一致させる。`argocd-diff` ワークフローは `on: pull_request` で実行されるため、`ref:refs/heads/main` ではなくこの形式が必要）
   - Custom Claim Rules: `{ "workflow": "ArgoCD Diff Check" }`（`.github/workflows/argocd-diff.yaml` の `name` フィールドに一致させる）
   - Tags: `tag:ci`
   - Scopes: `auth_keys`, `devices:core`
   - **最小権限の原則:** PoC 段階では `pull_request` イベント + 特定ワークフロー名に限定。Custom Claim Rules の `workflow` で対象ワークフローを制限する

2. **ACL ポリシー（例）:**
   ```json
   {
     "acls": [
       {
         "action": "accept",
         "src": ["tag:ci"],
         "dst": ["<argocd-service-clusterip>/32:443"]
       }
     ],
     "tagOwners": {
       "tag:ci": ["autogroup:admin"],
       "tag:argocd-api": ["autogroup:admin"]
     },
     "autoApprovers": {
       "routes": {
         "<argocd-service-clusterip>/32": ["tag:argocd-api"]
       }
     }
   }
   ```
   > **注意:** `dst` には subnet router がアドバタイズする CIDR/IP を直接指定する。タグ付きデバイス（`tag:argocd-api`）宛の指定では subnet route 配下の ClusterIP への通信を許可できないため、ArgoCD Service の実際の ClusterIP を使用すること。

3. **subnet routes 承認:**
   - subnet router がアドバタイズするルート（ArgoCD Service の ClusterIP 範囲、または特定 IP）を管理コンソールで承認

### 4.4 Terraform 適用範囲

PoC フェーズでは Terraform の適用は任意だが、以下を IaC 化する場合の対象:

| リソース | Terraform Provider | 備考 |
|---|---|---|
| Trust Credential | `tailscale_federated_identity` | 推奨: IaC 管理 |
| ACL ポリシー | `tailscale_acl` | 推奨: IaC 管理 |
| subnet router 承認 | `tailscale_device_subnet_routes` | 任意 |
| K8s マニフェスト | ArgoCD（既存パイプライン） | 既存フローに統合 |
| GitHub Secrets | `github_actions_secret` | 任意 |

### 4.5 検証項目

| # | 検証内容 | 期待結果 | 判定方法 |
|---|---|---|---|
| 1 | subnet router Pod が tailnet に参加 | `tailscale status` で表示される | Pod ログ + 管理コンソール |
| 2 | subnet routes が承認・有効化 | 管理コンソールで routes が green | 管理コンソール確認 |
| 3 | GitHub Actions が WIF で tailnet 接続 | ワークフローの tailscale step が成功 | Actions ログ |
| 4 | tailnet 内から ArgoCD API に到達 | HTTP 200 応答 | `curl` テスト |
| 5 | `argocd app diff` が正常実行 | 既存経路と同一結果 | diff 出力の比較 |
| 6 | エフェメラルノード自動削除 | ジョブ完了後にノードが消える | 管理コンソール確認 |
| 7 | 既存 Cloudflare 経路への影響なし | Cloudflare 経由の diff が正常動作 | 並行実行テスト |
| 8 | ACL で意図しないアクセスがブロックされる | ArgoCD 以外へのアクセスが拒否される | curl で他サービスへの接続テスト |

### 4.6 失敗時のロールバック手順

**レベル 1: ワークフローのロールバック（即座に実行可能）**

1. `argocd-diff.yaml` で Tailscale 接続パスをコメントアウトまたは削除
2. 既存の Cloudflare Service Token パスに戻す
3. 変更を commit・push → 次回の PR から Cloudflare 経路で動作

**レベル 2: subnet router の撤去（数分で実行可能）**

1. ArgoCD UI または CLI で Application を削除: `argocd app delete tailscale-subnet-router --cascade`
   - `--cascade` により、Application と管理対象リソース（Deployment, Service, Secret 等）が全て削除される
   - 代替手段: `kubectl delete -f argoproj/tailscale-subnet-router/` で直接マニフェストを削除
2. subnet router Pod が終了し、tailnet から自動的に切断される
3. Tailscale 管理コンソール → Machines から該当デバイスを手動削除（残存ノードのクリーンアップ）

**レベル 3: Tailscale 設定のクリーンアップ（任意のタイミング）**

1. Trust Credential を無効化または削除
2. ACL ポリシーから `tag:ci` 関連ルールを削除
3. GitHub Secrets/Variables から `TS_OAUTH_CLIENT_ID` / `TS_AUDIENCE` を削除

**各レベルの独立性:** レベル 1 のみで運用は完全に元に戻る。レベル 2-3 は不要なリソースのクリーンアップであり、緊急性は低い。

---

## 5. セキュリティ境界の整理

### 5.1 現状（Cloudflare Service Token）

```
GitHub Actions
  ├─ CF-Access-Client-Id (長寿命、GitHub Secrets に静的保存)
  ├─ CF-Access-Client-Secret (長寿命、GitHub Secrets に静的保存)
  └─ ARGOCD_AUTH_TOKEN (長寿命、GitHub Secrets に静的保存)
       ↓
  Cloudflare Edge (Service Token 検証)
       ↓
  Cloudflare Tunnel → K8s 内部 → ArgoCD API
```

**リスク:**
- Service Token は有効期限が長く、漏洩時に第三者が ArgoCD API にアクセス可能
- ローテーションが手動で、失効管理が属人的
- GitHub Secrets が侵害された場合、全てのシークレットが同時に露出

### 5.2 PoC 後（Tailscale WIF）

```
GitHub Actions
  ├─ OIDC Token (自動発行、短寿命、署名付き)
  ├─ TS_OAUTH_CLIENT_ID (非秘密情報)
  ├─ TS_AUDIENCE (非秘密情報)
  └─ ARGOCD_AUTH_TOKEN (長寿命 ← 将来の改善対象)
       ↓
  Tailscale Token Exchange (OIDC 検証、Subject 照合)
       ↓
  WireGuard Tunnel → subnet router → K8s 内部 → ArgoCD API
```

**改善点:**
- Service Token（長寿命秘密情報）が不要になる
- OIDC トークンは短寿命で自動発行、漏洩しても再利用困難
- Client ID / Audience は非秘密情報、漏洩しても認証には使えない
- WireGuard による GitHub Actions ランナー〜subnet router 間の暗号化（subnet router 以降は K8s 内部ネットワーク経由、ArgoCD 側の TLS で保護）
- Tailscale ACL によるきめ細かいアクセス制御

**残存リスク:**
- `ARGOCD_AUTH_TOKEN` は引き続き長寿命（PoC スコープ外だが、将来的に ArgoCD の OIDC 認証化で対応可能）
- subnet router の可用性が tailnet 経路の可用性に影響（Cloudflare 経路をフォールバックとして維持することで緩和）

---

## 付録

### A. テスト用ワークフローの概念設計

> **注意:** 以下は概念設計であり、実際のデプロイ時には `<argocd-service-clusterip>` を実際の ClusterIP に置換する必要がある。

```yaml
# .github/workflows/test-tailscale-wif.yaml
name: Test Tailscale WIF Connection
on:
  workflow_dispatch:
permissions:
  id-token: write
  contents: read
jobs:
  test-connection:
    runs-on: ubuntu-latest
    steps:
      - name: Connect to Tailscale via WIF
        uses: tailscale/github-action@v4
        with:
          oauth-client-id: ${{ vars.TS_OAUTH_CLIENT_ID }}
          audience: ${{ vars.TS_AUDIENCE }}
          tags: tag:ci
      - name: Verify tailnet connection
        run: tailscale status
      - name: Test L3/L4 reachability
        run: |
          # ネットワーク到達性テスト（TCP 接続のみ）
          # <argocd-service-clusterip> は実際のデプロイ時に置換
          timeout 10 bash -c 'cat < /dev/null > /dev/tcp/<argocd-service-clusterip>/443' || exit 1
      - name: Test ArgoCD API HTTP response
        run: |
          # HTTP レスポンステスト（TLS 検証をスキップ: ClusterIP では証明書 SAN が一致しないため）
          curl -skf --max-time 10 https://<argocd-service-clusterip>/api/version || exit 1
```

### B. subnet router Deployment の概念設計

> **注意:** subnet router はクラスター内の常駐 Pod であり、GitHub Actions のようにジョブ毎に OIDC トークンを取得する仕組みとは異なる。そのため、subnet router の tailnet 参加には Tailscale auth key（事前承認済み、再利用可能）を使用する。この auth key は AWS SSM Parameter Store → External Secrets Operator 経由で管理し、既存のシークレット管理パターンに統合する。将来的に Tailscale K8s Operator の WIF 対応（現在 beta）が GA になれば、K8s サービスアカウントの OIDC トークンで auth key も不要にできる。

```yaml
# argoproj/tailscale-subnet-router/deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: tailscale-subnet-router
  namespace: tailscale
spec:
  replicas: 1
  selector:
    matchLabels:
      app: tailscale-subnet-router
  template:
    metadata:
      labels:
        app: tailscale-subnet-router
    spec:
      serviceAccountName: tailscale-subnet-router
      containers:
        - name: tailscale
          image: tailscale/tailscale:latest
          env:
            - name: TS_AUTHKEY
              # 事前承認済み・再利用可能な auth key
              # AWS SSM → External Secrets Operator 経由で K8s Secret に同期
              valueFrom:
                secretKeyRef:
                  name: tailscale-auth
                  key: TS_AUTHKEY
            - name: TS_ROUTES
              # ArgoCD Service の ClusterIP のみ（例: 10.96.x.x/32）
              value: "<argocd-service-clusterip>/32"
            - name: TS_EXTRA_ARGS
              value: "--advertise-tags=tag:argocd-api"
          securityContext:
            capabilities:
              add: ["NET_ADMIN"]
```

### C. 参考リンク

- Tailscale Workload Identity Federation ドキュメント: `https://tailscale.com/docs/features/workload-identity-federation`
- Tailscale GitHub Action: `https://tailscale.com/docs/integrations/github/github-action`
- Tailscale ACL ドキュメント: `https://tailscale.com/kb/1018/acls`
- Tailscale Kubernetes Operator: `https://tailscale.com/docs/features/kubernetes-operator`
