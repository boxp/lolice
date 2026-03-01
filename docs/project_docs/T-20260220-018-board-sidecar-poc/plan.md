# T-20260220-018: board.md可視化 Phase 2 — サイドカー配信PoC

## 概要

board.md（OpenClaw Kanbanボード）をCloudflare Tunnel経由でWebブラウザから閲覧可能にする最小構成PoC。

## アーキテクチャ

```
ブラウザ → Cloudflare Access (GitHub認証)
         → Cloudflare Tunnel
         → cloudflared Pod (namespace: openclaw)
         → openclaw Service :8080
         → openclaw Pod / board-server サイドカー (nginx :8080)
           └── PVC openclaw-data (readOnly) → /data/workspace/tasks/board.md
```

## 変更対象

### 1. boxp/lolice (K8sマニフェスト)

#### 1.1 新規: `configmap-board-server.yaml`
- **nginx.conf**: ポート8080でリッスン、`/api/board.md` でMarkdown生ファイル配信（`Cache-Control: no-store` 付与）、`/` でindex.html配信
- **index.html**: ConfigMap内蔵のmarked.js + DOMPurify でMarkdown→HTML変換・サニタイズ、5秒ポーリングで自動更新
- **style.css**: 外部CSSファイルとしてスタイル定義（`'unsafe-inline'` 回避）
- **marked.min.js / purify.min.js**: CDN依存を排し、ConfigMap内にバンドルして `'self'` スコープで配信

#### 1.2 変更: `deployment-openclaw.yaml`
- `board-server` サイドカーコンテナ追加 (nginx:1.27-alpine)
- PVC `data` を `/data` にreadOnlyマウント
- ConfigMap `board-server-config` を `/etc/nginx/conf.d/` と `/usr/share/nginx/html/` にマウント

#### 1.3 変更: `service-openclaw.yaml`
- ポート8080 (board) を追加

#### 1.4 変更: `networkpolicy.yaml`
- cloudflared → openclaw Pod :8080 の通信を許可
- 注: 既存NetworkPolicyは `policyTypes: [Ingress, Egress]` を明示しており、リスト外のポートへの通信はdefault denyとなる

#### 1.5 変更: `kustomization.yaml`
- `configmap-board-server.yaml` をリソースに追加

### 2. boxp/arch (Terraform)

#### 2.1 変更: `tunnel.tf`
- `board.b0xp.io` → `http://openclaw.openclaw.svc.cluster.local:8080` のingress_rule追加

#### 2.2 変更: `dns.tf`
- `board.b0xp.io` のCNAMEレコード追加

#### 2.3 変更: `access.tf`
- `board.b0xp.io` 用のAccess applicationとpolicy追加（既存のGitHub IdP認証ポリシーを踏襲）

## セキュリティ多層防御

| レイヤー | 対策 |
|---------|------|
| L1 | Cloudflare Access (GitHub IdP認証のみ許可) |
| L2 | CSP ヘッダー (`script-src 'self'` — 外部CDN不使用) |
| L3 | DOMPurify によるXSSサニタイズ |
| L4 | nginx readOnlyマウント (board.mdの改ざん防止) |
| L5 | NetworkPolicy (policyTypes明示により default deny、cloudflared → :8080 のみ Ingress許可) |

## CSP方針

```
Content-Security-Policy:
  default-src 'self';
  script-src 'self';
  style-src 'self';
  img-src https: data:;
  font-src 'self';
  frame-ancestors 'none';
  base-uri 'self';
  form-action 'self'
```

- `script-src 'self'`: JSはConfigMap内蔵のため外部CDN不要。inline scriptも禁止
- `style-src 'self'`: 外部CSSファイル（style.css）のみ。`'unsafe-inline'` 不使用
- `frame-ancestors 'none'`: クリックジャッキング防止

## HTTPキャッシュ制御

`/api/board.md` レスポンスには以下のヘッダーを付与し、Cloudflare Edge・ブラウザ双方のキャッシュを抑止する:

```
Cache-Control: no-store, no-cache, must-revalidate
Pragma: no-cache
Cloudflare-CDN-Cache-Control: no-store
```

nginx側で `open_file_cache off` も設定し、ファイルディスクリプタキャッシュも無効化。

## 更新反映SLO

- **目標**: 最悪ケース <= 10秒 (board.md変更からWeb反映まで)
- **メカニズム**: フロントエンドが5秒間隔で `/api/board.md` をfetchでポーリング。nginx `open_file_cache off` + `Cache-Control: no-store` によりPVC書き込みが即座にレスポンスに反映。Cloudflare Edgeキャッシュも `CDN-Cache-Control: no-store` で抑止。
- **理論値**: 0〜5秒 (ポーリング間隔) + ネットワーク遅延 ≈ 1〜6秒
- **検証方法**: docs/slo-verification.md に記載

## ロールバック手順

1. lolice: `deployment-openclaw.yaml` からboard-serverサイドカーを削除、ConfigMap・Service変更を巻き戻し
2. arch: `tunnel.tf`/`dns.tf`/`access.tf` からboard関連リソースを削除
3. `kubectl apply` / `terraform apply` で反映
