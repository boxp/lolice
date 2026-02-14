# Codex CLI 認証情報永続化のための PV 設定

## 概要
OpenClaw 内で利用する `codex cli` の認証情報（`~/.codex` フォルダ）を永続化するために、
既存の Kubernetes PersistentVolumeClaim (`openclaw-data`) を活用してマウントを追加する。

## 現状分析

### コンテナの実行ユーザー
- UID: 1000 (node)
- GID: 1000
- `~/.codex` は `/home/node/.codex` に対応

### 既存の PV/PVC 構成
- PVC `openclaw-data`: 10Gi, Longhorn, ReadWriteOnce
- マウント済みパス:
  - `/home/node/.openclaw` (data volume, root)
  - `/home/node/ghq` (data volume, subPath: ghq)

## 計画

### 方針: 既存 PVC に subPath マウントを追加
新規 PVC を作成せず、既存の `openclaw-data` PVC に `codex` subPath でマウントを追加する。

**理由:**
- 10Gi の容量は `~/.codex` の認証データ（数KB〜数MB）を含めても十分
- 管理が簡潔（PVC を増やさない）
- 既に `ghq` で同じ subPath パターンを使用しているため一貫性がある

### 修正対象ファイル
- `argoproj/openclaw/deployment-openclaw.yaml` のみ

### 修正内容

#### deployment-openclaw.yaml
1. **openclaw コンテナ** の `volumeMounts` に追加:
   ```yaml
   - name: data
     mountPath: /home/node/.codex
     subPath: codex
   ```

2. **init-config コンテナ** の args に `~/.codex` ディレクトリ初期化を追加:
   ```yaml
   mkdir -p /home/node/.codex
   ```
   および対応する volumeMount を追加。

### セキュリティ
- fsGroup: 1000 により、PV 上のファイルは GID 1000 でアクセス可能
- 既存のセキュリティコンテキストで問題なし
- `~/.codex` は認証トークンを含むため、ReadWriteOnce で単一 Pod のみアクセス可能な現行構成が適切

### 影響範囲
- OpenClaw Pod の再起動が必要（Recreate strategy のため自動で対応）
- 他のコンポーネント（LiteLLM, Cloudflared）への影響なし
- PVC の定義変更なし（kustomization.yaml の変更不要）
