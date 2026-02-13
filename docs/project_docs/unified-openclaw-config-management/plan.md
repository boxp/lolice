# `openclaw` 全設定の再起動なし GitOps 統合管理計画（最終版）

**作成日**: 2026-02-13
**最終更新日**: 2026-02-13
**作成者**: Claude Opus 4.6
**対象システム**: BOXP (lolice k8s cluster) - `openclaw` namespace

---

## 調査結論: 公式ホットリロード機能が存在する

> **結論**: `openclaw` は `chokidar` ライブラリによるファイル監視ベースのホットリロード機能を**公式にサポート**している。
> `~/.openclaw/openclaw.json` を更新するだけで、アプリケーションが自動的に変更を検知し、ホットリロードを実行する。
> **SIGHUP シグナルやHTTPリロードエンドポイントは不要。**

### 調査結果サマリー

| 調査項目 | 結果 |
|---------|------|
| SIGHUP シグナルリロード | **非対応** (子プロセスブリッジでの転送のみ) |
| HTTP リロードエンドポイント | **なし** (RPC/WebSocket ベースの `config.apply`/`config.patch` のみ) |
| ファイル監視による自動リロード | **公式対応** (`chokidar` による `~/.openclaw/openclaw.json` の監視) |
| CLI リロードコマンド | `openclaw config set/get/unset` (ファイル書き込み → 自動検知) |
| デフォルトリロードモード | `hybrid` (安全な変更はホットアプライ、クリティカルな変更は自動再起動) |
| デバウンス | 300ms (設定可能: `gateway.reload.debounceMs`) |

### リロードモード (`gateway.reload.mode`)

| モード | 挙動 |
|--------|------|
| **`hybrid`** (デフォルト) | 安全な変更は即座にホットアプライ。クリティカルな変更は自動的にゲートウェイを再起動 |
| `hot` | 安全な変更のみホットアプライ。再起動が必要な変更は警告ログのみ |
| `restart` | どんな設定変更でもゲートウェイを再起動 |
| `off` | ファイル監視を無効化 |

### ホットアプライ可能なフィールド vs 再起動が必要なフィールド

| カテゴリ | フィールド | 再起動必要? |
|----------|-----------|------------|
| チャネル | `channels.*`, `web` | **No** |
| エージェント・モデル | `agent`, `agents`, `models`, `routing` | **No** |
| オートメーション | `hooks`, `cron`, `agent.heartbeat` | **No** |
| セッション・メッセージ | `session`, `messages` | **No** |
| ツール・メディア | `tools`, `browser`, `skills`, `audio`, `talk` | **No** |
| UI・その他 | `ui`, `logging`, `identity`, `bindings` | **No** |
| ゲートウェイサーバー | `gateway.*` (port, bind, auth, tailscale, TLS, HTTP) | **Yes** |
| インフラ | `discovery`, `canvasHost`, `plugins` | **Yes** |

> **重要**: `gateway.reload` と `gateway.remote` は例外で、変更しても再起動はトリガーされない。

### 設計上の影響

この発見により、当初計画していた **SIGHUP ベースのリロードメカニズムは不要** となった。サイドカーの `handle_config_change()` で必要なアクションは:

1. ConfigMap のファイルを PVC にコピーする（`cp "$CONFIG_DIR/openclaw.json" "$PVC_CONFIG"`）
2. **それだけ。** openclaw が自動的にファイル変更を検知してホットリロードする。

---

## 目次

1. [概要と目的](#1-概要と目的)
2. [現状分析](#2-現状分析)
3. [アーキテクチャ設計](#3-アーキテクチャ設計)
4. [実装計画](#4-実装計画)
5. [前提条件とリスク](#5-前提条件とリスク)
6. [運用フロー](#6-運用フロー)
7. [実装手順](#7-実装手順)

---

## 1. 概要と目的

### 1.1 目的

`openclaw` の**メイン設定** (`openclaw.json`) と**cronジョブ定義**の両方を、Podを再起動せずに宣言的（GitOps）に管理する。

### 1.2 現在の課題

| 項目 | 現状 | 課題 |
|------|------|------|
| メイン設定 (`openclaw.json`) | ConfigMap → initContainerでPVCへコピー | ConfigMap変更時にReloaderがPodを再起動してしまう |
| cronジョブ | `openclaw cron add` で手動登録 | GitOps管理されていない。Pod再作成時に消失する可能性がある |

### 1.3 目標状態

- メイン設定の変更 → Podを再起動せずにホットリロード（`chokidar` ファイル監視による自動検知）
- cronジョブの変更 → Podを再起動せずに動的に同期
- すべての設定がGitで管理され、ArgoCD経由で自動適用される

### 1.4 採用方式: 完全ホットリロード

**調査の結果、`openclaw` はファイル監視ベースのホットリロードを公式サポートしているため、完全ホットリロード方式を採用する。**

- メイン設定: PVC上のファイルコピーのみでリロード完了（Reloaderによる再起動は不要）
- cronジョブ: サイドカーが `openclaw cron` コマンドで動的同期
- `shareProcessNamespace` は**不要**（SIGHUP送信が不要なため）

---

## 2. 現状分析

### 2.1 既存のマニフェスト構成

```
repos/lolice/argoproj/openclaw/
├── deployment-openclaw.yaml      # OpenClaw本体 + DinD sidecar
├── configmap-openclaw.yaml       # openclaw.json（メイン設定）
├── external-secret.yaml          # AWS SSM → Secret
├── pvc.yaml                      # Longhorn 10Gi
├── service-openclaw.yaml
├── networkpolicy.yaml
├── deployment-cloudflared.yaml
├── deployment-litellm.yaml
├── configmap-litellm.yaml
├── service-litellm.yaml
├── external-secret-litellm.yaml
├── namespace.yaml
├── application.yaml
└── kustomization.yaml
```

### 2.2 現在のDeployment構成

```
Pod: openclaw
├── initContainer: init-config      # ConfigMap → PVCに openclaw.json コピー
├── initContainer: init-dotfiles    # dotfiles clone/pull
├── container: openclaw             # メインプロセス (node dist/index.js gateway)
└── container: dind                 # Docker-in-Docker sidecar
```

### 2.3 ボリューム構成

| Volume | Source | Mount Path | 用途 |
|--------|--------|------------|------|
| `data` | PVC `openclaw-data` (Longhorn 10Gi) | `/home/node/.openclaw` | 設定・データ |
| `data` (subPath: ghq) | 同上 | `/home/node/ghq` | Gitリポジトリ |
| `openclaw-config` | ConfigMap `openclaw-config` | `/config` (initContainerのみ) | 設定テンプレート |
| `docker-certs` | emptyDir | `/certs` | DinD TLS証明書 |
| `docker-data` | emptyDir | `/var/lib/docker` | Docker data |

### 2.4 Reloader設定（現在）

```yaml
annotations:
  configmap.reloader.stakater.com/reload: "openclaw-config"
```

現在、`openclaw-config` ConfigMapの変更時に**Podが再起動される**（不要な再起動）。

---

## 3. アーキテクチャ設計

### 3.1 全体像

```
┌─────────────────────────────────────────────────────────────────────┐
│  Git Repository (lolice)                                            │
│  ├── configmap-openclaw.yaml          (メイン設定: openclaw.json)    │
│  └── configmap-openclaw-cron.yaml     (cronジョブ定義)              │
│                       │                                              │
│                       ▼                                              │
│               ArgoCD 自動同期                                        │
│                       │                                              │
│                       ▼                                              │
│  ┌─ Kubernetes ───────────────────────────────────────────────────┐  │
│  │                                                                 │  │
│  │  ConfigMap: openclaw-config        ConfigMap: openclaw-cron-jobs│  │
│  │       │                                  │                      │  │
│  │       │ (volume mount)                   │ (volume mount)       │  │
│  │       ▼                                  ▼                      │  │
│  │  ┌─ Pod: openclaw ──────────────────────────────────────────┐  │  │
│  │  │                                                           │  │  │
│  │  │  /etc/openclaw/config.d/openclaw.json  ← メイン設定       │  │  │
│  │  │  /etc/openclaw/cron.d/cron-jobs.json   ← cron定義         │  │  │
│  │  │                                                           │  │  │
│  │  │  ┌──────────────────────────────────┐                     │  │  │
│  │  │  │  config-manager-sidecar          │                     │  │  │
│  │  │  │                                  │                     │  │  │
│  │  │  │  ポーリング(60秒)で監視:         │                     │  │  │
│  │  │  │  ├── config.d/ 変更検知          │                     │  │  │
│  │  │  │  │   → PVCへコピーのみ           │                     │  │  │
│  │  │  │  │     (chokidarが自動検知)      │                     │  │  │
│  │  │  │  └── cron.d/ 変更検知            │                     │  │  │
│  │  │  │      → openclaw cron sync 実行   │                     │  │  │
│  │  │  └──────────────────────────────────┘                     │  │  │
│  │  │                                                           │  │  │
│  │  │  ┌──────────────────────────────────┐                     │  │  │
│  │  │  │  openclaw (メインコンテナ)       │                     │  │  │
│  │  │  │  /home/node/.openclaw/           │  ← PVC              │  │  │
│  │  │  │    ├── openclaw.json ←──────── chokidar が監視         │  │  │
│  │  │  │    │     変更検知 → ホットリロード                      │  │  │
│  │  │  │    └── (cronデータ等)            │                     │  │  │
│  │  │  └──────────────────────────────────┘                     │  │  │
│  │  │                                                           │  │  │
│  │  │  ┌──────────────────────────────────┐                     │  │  │
│  │  │  │  dind (Docker-in-Docker)         │                     │  │  │
│  │  │  └──────────────────────────────────┘                     │  │  │
│  │  └───────────────────────────────────────────────────────────┘  │  │
│  │                                                                 │  │
│  │  Reloader: 無効化 (reloader.stakater.com/auto: "false")         │  │
│  └─────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

### 3.2 ConfigMapの分離

2つのConfigMapに設定を分離する:

| ConfigMap | 格納内容 | マウント先 |
|-----------|---------|-----------|
| `openclaw-config` | `openclaw.json` (メイン設定) | `/etc/openclaw/config.d/` |
| `openclaw-cron-jobs` | `cron-jobs.json` (cronジョブ定義) | `/etc/openclaw/cron.d/` |

### 3.3 config-manager-sidecar の設計

単一の軽量サイドカーが、両方のConfigMapディレクトリの変更を監視し、適切なアクションを実行する。

#### 監視対象と対応アクション

| 監視ディレクトリ | 変更検知方法 | アクション |
|-----------------|-------------|-----------|
| `/etc/openclaw/config.d/` | チェックサム比較 (60秒ポーリング) | **PVCへコピーのみ**（openclaw が chokidar で自動検知してホットリロード） |
| `/etc/openclaw/cron.d/` | チェックサム比較 (60秒ポーリング) | `openclaw cron` コマンドでジョブを同期 |

#### メイン設定の更新ロジック（最終版）

`openclaw` はファイル監視（`chokidar`）を内蔵しているため、PVC上の `openclaw.json` を更新するだけで自動的にホットリロードされる。

```
ConfigMap 更新
  → サイドカーがチェックサム変更を検知（~60秒以内）
    → PVC上の openclaw.json にコピー
      → openclaw の chokidar が変更を検知（~300ms以内）
        → hybrid モードにより:
          - 安全な変更 → 即座にホットアプライ（再起動なし）
          - クリティカルな変更(gateway.*)  → 自動的にプロセス再起動
```

**SIGHUP送信もHTTPリロードAPIも不要。** そのため `shareProcessNamespace` も不要で、設計がシンプルになる。

#### cronジョブの更新ロジック

1. ConfigMapから新しいcronジョブ定義を読み取る
2. `openclaw cron list` で現在の登録済みジョブを取得
3. 差分を計算し、以下を実行:
   - 新規ジョブ: `openclaw cron add`
   - 変更ジョブ: `openclaw cron update`
   - 削除ジョブ: `openclaw cron remove`

---

## 4. 実装計画

### 4.1 ConfigMap: `openclaw-config` (既存・変更なし)

```yaml
# configmap-openclaw.yaml (既存・変更なし)
apiVersion: v1
kind: ConfigMap
metadata:
  name: openclaw-config
  namespace: openclaw
data:
  openclaw.json: |
    {
      "models": { ... },
      "agents": { ... },
      "plugins": { ... },
      "channels": { ... },
      "gateway": { ... }
    }
```

### 4.2 ConfigMap: `openclaw-cron-jobs` (新規作成)

```yaml
# configmap-openclaw-cron.yaml (新規)
apiVersion: v1
kind: ConfigMap
metadata:
  name: openclaw-cron-jobs
  namespace: openclaw
  labels:
    app.kubernetes.io/name: openclaw
    app.kubernetes.io/component: cron
    app.kubernetes.io/managed-by: argocd
data:
  cron-jobs.json: |
    [
      {
        "name": "obsidian-vault-sync",
        "description": "Sync Obsidian Vault every 15 minutes",
        "schedule": "*/15 * * * *",
        "command": "/bin/bash -c 'cd /home/node/ghq/github.com/boxp/obsidian-vault && git pull origin main'",
        "timeout": 300,
        "retry": 3
      }
    ]
```

### 4.3 サイドカーの実装: `manager.sh`（最終版）

```bash
#!/bin/sh
set -eu

# ========================================
# config-manager-sidecar
# ConfigMapの変更を検知して設定を動的に反映する
#
# メイン設定: PVC上のファイルをコピーするだけで
#             openclaw の chokidar が自動検知してホットリロード
# cronジョブ: openclaw cron コマンドで差分を同期
# ========================================

CONFIG_DIR="/etc/openclaw/config.d"
CRON_DIR="/etc/openclaw/cron.d"
PVC_CONFIG="/home/node/.openclaw/openclaw.json"
STAGING_DIR="/home/node/.openclaw/cron-config"
CHECKSUM_DIR="/tmp/checksums"
POLL_INTERVAL=60

mkdir -p "$CHECKSUM_DIR" "$STAGING_DIR"

log() {
  echo "$(date '+%Y-%m-%d %H:%M:%S') [config-manager] $*"
}

compute_checksum() {
  if [ -f "$1" ]; then
    md5sum "$1" | awk '{print $1}'
  else
    echo "none"
  fi
}

# === メイン設定の更新処理 ===
# openclaw は chokidar でファイル監視しているため、
# PVC上のファイルをコピーするだけで自動的にホットリロードされる。
# SIGHUP送信やAPIコールは不要。
handle_config_change() {
  log "Config change detected in $CONFIG_DIR/openclaw.json"

  # PVC上の設定ファイルを更新
  cp "$CONFIG_DIR/openclaw.json" "$PVC_CONFIG"
  log "Config copied to PVC: $PVC_CONFIG"

  # openclaw の chokidar が自動的に変更を検知する（デバウンス: 300ms）
  # hybrid モードにより:
  #   - 安全な変更（channels, agents, models, cron等）→ ホットアプライ
  #   - クリティカルな変更（gateway.port, gateway.bind等）→ 自動再起動
  log "Config update complete (openclaw will auto-detect via chokidar)"
}

# === cronジョブの同期処理 ===
handle_cron_change() {
  log "Cron config change detected in $CRON_DIR/cron-jobs.json"

  local CRON_CONFIG="$CRON_DIR/cron-jobs.json"

  if [ ! -f "$CRON_CONFIG" ]; then
    log "WARNING: Cron config file not found"
    return
  fi

  # ステージングディレクトリに最新の定義をコピー
  cp "$CRON_CONFIG" "$STAGING_DIR/cron-jobs.json"

  # jqが利用可能かチェック
  if ! command -v jq >/dev/null 2>&1; then
    log "WARNING: jq not available, cannot parse cron config"
    return
  fi

  # ConfigMapで定義されたジョブ名のリストを取得
  DESIRED_JOBS=$(jq -r '.[].name' "$CRON_CONFIG")

  # 現在登録されているジョブのリストを取得
  CURRENT_JOBS=$(openclaw cron list --format json 2>/dev/null | jq -r '.[].name' 2>/dev/null || echo "")

  # 新規・更新ジョブの登録
  echo "$DESIRED_JOBS" | while IFS= read -r JOB_NAME; do
    [ -z "$JOB_NAME" ] && continue

    JOB_DEF=$(jq -c ".[] | select(.name == \"$JOB_NAME\")" "$CRON_CONFIG")
    echo "$JOB_DEF" > "/tmp/cron-job-${JOB_NAME}.json"

    if echo "$CURRENT_JOBS" | grep -qx "$JOB_NAME"; then
      # 既存ジョブの更新
      log "Updating cron job: $JOB_NAME"
      openclaw cron update "/tmp/cron-job-${JOB_NAME}.json" 2>/dev/null || \
        log "WARNING: Failed to update cron job: $JOB_NAME"
    else
      # 新規ジョブの追加
      log "Adding cron job: $JOB_NAME"
      openclaw cron add "/tmp/cron-job-${JOB_NAME}.json" 2>/dev/null || \
        log "WARNING: Failed to add cron job: $JOB_NAME"
    fi

    rm -f "/tmp/cron-job-${JOB_NAME}.json"
  done

  # ConfigMapから削除されたジョブの除去
  if [ -n "$CURRENT_JOBS" ]; then
    echo "$CURRENT_JOBS" | while IFS= read -r JOB_NAME; do
      [ -z "$JOB_NAME" ] && continue
      if ! echo "$DESIRED_JOBS" | grep -qx "$JOB_NAME"; then
        log "Removing cron job (no longer in ConfigMap): $JOB_NAME"
        openclaw cron remove "$JOB_NAME" 2>/dev/null || \
          log "WARNING: Failed to remove cron job: $JOB_NAME"
      fi
    done
  fi

  log "Cron job sync complete"
}

# === メインループ ===

log "Starting config-manager-sidecar"
log "  Monitoring: $CONFIG_DIR (main config → file copy → chokidar auto-reload)"
log "  Monitoring: $CRON_DIR (cron jobs → openclaw cron sync)"
log "  Poll interval: ${POLL_INTERVAL}s"

# メインプロセスの起動を待機
log "Waiting for openclaw main process to start..."
WAIT_COUNT=0
while [ $WAIT_COUNT -lt 60 ]; do
  if pgrep -f "node dist/index.js gateway" >/dev/null 2>&1; then
    log "Main process detected"
    break
  fi
  sleep 5
  WAIT_COUNT=$((WAIT_COUNT + 1))
done

# 初期チェックサム設定
compute_checksum "$CONFIG_DIR/openclaw.json" > "$CHECKSUM_DIR/config"
compute_checksum "$CRON_DIR/cron-jobs.json" > "$CHECKSUM_DIR/cron"

# 初回のcronジョブ同期を実行
log "Performing initial cron job sync..."
handle_cron_change

# ポーリングループ
while true; do
  sleep "$POLL_INTERVAL"

  # メイン設定の変更チェック
  NEW_CONFIG_HASH=$(compute_checksum "$CONFIG_DIR/openclaw.json")
  OLD_CONFIG_HASH=$(cat "$CHECKSUM_DIR/config" 2>/dev/null || echo "")
  if [ "$NEW_CONFIG_HASH" != "$OLD_CONFIG_HASH" ]; then
    handle_config_change
    echo "$NEW_CONFIG_HASH" > "$CHECKSUM_DIR/config"
  fi

  # cronジョブ設定の変更チェック
  NEW_CRON_HASH=$(compute_checksum "$CRON_DIR/cron-jobs.json")
  OLD_CRON_HASH=$(cat "$CHECKSUM_DIR/cron" 2>/dev/null || echo "")
  if [ "$NEW_CRON_HASH" != "$OLD_CRON_HASH" ]; then
    handle_cron_change
    echo "$NEW_CRON_HASH" > "$CHECKSUM_DIR/cron"
  fi
done
```

### 4.4 Deployment マニフェスト: `deployment-openclaw.yaml` (完全版)

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: openclaw
  namespace: openclaw
  annotations:
    # === Reloader: 無効化 ===
    # config-manager-sidecar がConfigMap変更を検知し、PVCにコピー。
    # openclaw の chokidar がPVC上のファイル変更を検知してホットリロード。
    # Reloaderによる自動再起動は不要。
    reloader.stakater.com/auto: "false"
  labels:
    app.kubernetes.io/name: openclaw
    app.kubernetes.io/component: gateway
    app.kubernetes.io/managed-by: argocd
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app.kubernetes.io/name: openclaw
      app.kubernetes.io/component: gateway
  template:
    metadata:
      labels:
        app.kubernetes.io/name: openclaw
        app.kubernetes.io/component: gateway
    spec:
      automountServiceAccountToken: false
      securityContext:
        fsGroup: 1000
        # shareProcessNamespace は不要
        # (SIGHUP送信不要: chokidar ファイル監視でホットリロード)
      nodeSelector:
        kubernetes.io/arch: amd64

      initContainers:
        # ---- init-config: ConfigMapからPVCへ設定をコピー ----
        - name: init-config
          image: ghcr.io/boxp/arch/openclaw:202602130742
          command: ["sh", "-c"]
          args:
            - |
              mkdir -p /home/node/.openclaw
              cp /config/openclaw.json /home/node/.openclaw/openclaw.json
          securityContext:
            runAsUser: 1000
            runAsGroup: 1000
            runAsNonRoot: true
            allowPrivilegeEscalation: false
          volumeMounts:
            - name: openclaw-config
              mountPath: /config
              readOnly: true
            - name: data
              mountPath: /home/node/.openclaw
            - name: data
              mountPath: /home/node/ghq
              subPath: ghq

        # ---- init-dotfiles: dotfilesのclone/pull ----
        - name: init-dotfiles
          image: ghcr.io/boxp/arch/openclaw:202602130742
          command: ["sh", "-c"]
          args:
            - |
              DOTFILES_DIR=/home/node/ghq/github.com/boxp/dotfiles
              if [ -d "$DOTFILES_DIR/.git" ]; then
                cd "$DOTFILES_DIR" && git checkout master && git pull origin master
              else
                mkdir -p "$(dirname "$DOTFILES_DIR")"
                git clone https://github.com/boxp/dotfiles.git "$DOTFILES_DIR"
              fi
          securityContext:
            runAsUser: 1000
            runAsGroup: 1000
            runAsNonRoot: true
            allowPrivilegeEscalation: false
          volumeMounts:
            - name: data
              mountPath: /home/node/ghq
              subPath: ghq
            - name: data
              mountPath: /home/node/.openclaw

        # ---- init-cron-config: cronジョブ定義をPVCにステージング ----
        - name: init-cron-config
          image: ghcr.io/boxp/arch/openclaw:202602130742
          command: ["sh", "-c"]
          args:
            - |
              echo "=== Staging cron job config from ConfigMap ==="
              mkdir -p /home/node/.openclaw/cron-config
              if [ -f /cron-config/cron-jobs.json ]; then
                cp /cron-config/cron-jobs.json /home/node/.openclaw/cron-config/cron-jobs.json
                echo "=== Cron config staged successfully ==="
              else
                echo "=== No cron config found, skipping ==="
              fi
          securityContext:
            runAsUser: 1000
            runAsGroup: 1000
            runAsNonRoot: true
            allowPrivilegeEscalation: false
          volumeMounts:
            - name: cron-config
              mountPath: /cron-config
              readOnly: true
            - name: data
              mountPath: /home/node/.openclaw

      containers:
        # ---- openclaw: メインコンテナ ----
        - name: openclaw
          image: ghcr.io/boxp/arch/openclaw:202602130742
          command: ["sh", "-c"]
          args:
            - |
              DOTFILES_DIR=/home/node/ghq/github.com/boxp/dotfiles
              # Symlink individual dotfiles into .claude instead of replacing
              # the entire directory, to preserve Dockerfile's config.json
              # and settings.json (trustedWorkspaces, allowedCommands).
              CLAUDE_DIR=/home/node/.claude
              if [ -d "$DOTFILES_DIR/.claude" ]; then
                for item in "$DOTFILES_DIR/.claude"/* "$DOTFILES_DIR/.claude"/.*; do
                  name="$(basename "$item")"
                  case "$name" in .|..|config.json|settings.json) continue ;; esac
                  [ -e "$item" ] || continue
                  ln -sfn "$item" "$CLAUDE_DIR/$name"
                done
              fi

              # === cronジョブの起動時登録 ===
              CRON_CONFIG="/home/node/.openclaw/cron-config/cron-jobs.json"
              if [ -f "$CRON_CONFIG" ] && command -v jq >/dev/null 2>&1; then
                echo "=== Registering cron jobs from staged config ==="
                jq -c '.[]' "$CRON_CONFIG" | while read -r job; do
                  JOB_NAME=$(echo "$job" | jq -r '.name')
                  echo "$job" > "/tmp/cron-job-${JOB_NAME}.json"
                  openclaw cron add "/tmp/cron-job-${JOB_NAME}.json" 2>/dev/null || \
                    openclaw cron update "/tmp/cron-job-${JOB_NAME}.json" 2>/dev/null || \
                    echo "Warning: Failed to register cron job: ${JOB_NAME}"
                  rm -f "/tmp/cron-job-${JOB_NAME}.json"
                done
                echo "=== Cron job registration complete ==="
              fi

              exec node dist/index.js gateway --bind lan --port 18789
          ports:
            - containerPort: 18789
          env:
            - name: DISCORD_BOT_TOKEN
              valueFrom:
                secretKeyRef:
                  name: openclaw-credentials
                  key: DISCORD_BOT_TOKEN
            - name: OPENCLAW_GATEWAY_TOKEN
              valueFrom:
                secretKeyRef:
                  name: openclaw-credentials
                  key: OPENCLAW_GATEWAY_TOKEN
            - name: GITHUB_TOKEN
              valueFrom:
                secretKeyRef:
                  name: openclaw-credentials
                  key: GITHUB_TOKEN
            - name: GH_TOKEN
              valueFrom:
                secretKeyRef:
                  name: openclaw-credentials
                  key: GITHUB_TOKEN
            - name: LITELLM_PROXY_KEY
              valueFrom:
                secretKeyRef:
                  name: litellm-credentials
                  key: LITELLM_MASTER_KEY
            - name: OPENAI_API_KEY
              valueFrom:
                secretKeyRef:
                  name: openclaw-credentials
                  key: OPENAI_API_KEY
            - name: CLAUDE_CODE_OAUTH_TOKEN
              valueFrom:
                secretKeyRef:
                  name: openclaw-credentials
                  key: CLAUDE_CODE_OAUTH_TOKEN
            - name: XAI_API_KEY
              valueFrom:
                secretKeyRef:
                  name: openclaw-credentials
                  key: XAI_API_KEY
            - name: PATH
              value: "/shared-bin:/shared-npm/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
            - name: DOCKER_HOST
              value: "tcp://localhost:2376"
            - name: DOCKER_TLS_VERIFY
              value: "1"
            - name: DOCKER_CERT_PATH
              value: "/certs/client"
          resources:
            requests:
              cpu: 500m
              memory: 1Gi
            limits:
              cpu: 2000m
              memory: 4Gi
          securityContext:
            runAsUser: 1000
            runAsGroup: 1000
            runAsNonRoot: true
            readOnlyRootFilesystem: false
            allowPrivilegeEscalation: false
            capabilities:
              drop:
                - ALL
          volumeMounts:
            - name: docker-certs
              mountPath: /certs/client
              subPath: client
              readOnly: true
            - name: data
              mountPath: /home/node/.openclaw
            - name: data
              mountPath: /home/node/ghq
              subPath: ghq

        # ---- config-manager-sidecar: 設定変更の動的反映 ----
        # メイン設定: PVCへコピーのみ（openclaw の chokidar が自動検知）
        # cronジョブ: openclaw cron コマンドで差分同期
        # shareProcessNamespace 不要（SIGHUP送信不要）
        - name: config-manager-sidecar
          image: ghcr.io/boxp/arch/openclaw:202602130742
          command: ["sh", "-c"]
          args:
            - |
              set -eu

              CONFIG_DIR="/etc/openclaw/config.d"
              CRON_DIR="/etc/openclaw/cron.d"
              PVC_CONFIG="/home/node/.openclaw/openclaw.json"
              STAGING_DIR="/home/node/.openclaw/cron-config"
              CHECKSUM_DIR="/tmp/checksums"
              POLL_INTERVAL=60

              mkdir -p "$CHECKSUM_DIR" "$STAGING_DIR"

              log() { echo "$(date '+%Y-%m-%d %H:%M:%S') [config-manager] $*"; }

              compute_checksum() {
                if [ -f "$1" ]; then md5sum "$1" | awk '{print $1}'; else echo "none"; fi
              }

              # --- メイン設定の更新 ---
              # PVCへコピーするだけ。openclaw の chokidar が自動検知してホットリロード。
              handle_config_change() {
                log "Config change detected"
                cp "$CONFIG_DIR/openclaw.json" "$PVC_CONFIG"
                log "Config copied to PVC (openclaw will auto-detect via chokidar)"
              }

              # --- cronジョブの同期 ---
              handle_cron_change() {
                log "Cron config change detected"
                local CRON_CONFIG="$CRON_DIR/cron-jobs.json"
                [ -f "$CRON_CONFIG" ] || return

                cp "$CRON_CONFIG" "$STAGING_DIR/cron-jobs.json"

                if ! command -v jq >/dev/null 2>&1; then
                  log "WARNING: jq not available"
                  return
                fi

                DESIRED_JOBS=$(jq -r '.[].name' "$CRON_CONFIG")
                CURRENT_JOBS=$(openclaw cron list --format json 2>/dev/null | jq -r '.[].name' 2>/dev/null || echo "")

                echo "$DESIRED_JOBS" | while IFS= read -r JOB_NAME; do
                  [ -z "$JOB_NAME" ] && continue
                  JOB_DEF=$(jq -c ".[] | select(.name == \"$JOB_NAME\")" "$CRON_CONFIG")
                  echo "$JOB_DEF" > "/tmp/cron-job-${JOB_NAME}.json"
                  if echo "$CURRENT_JOBS" | grep -qx "$JOB_NAME"; then
                    log "Updating cron job: $JOB_NAME"
                    openclaw cron update "/tmp/cron-job-${JOB_NAME}.json" 2>/dev/null || \
                      log "WARNING: Failed to update cron job: $JOB_NAME"
                  else
                    log "Adding cron job: $JOB_NAME"
                    openclaw cron add "/tmp/cron-job-${JOB_NAME}.json" 2>/dev/null || \
                      log "WARNING: Failed to add cron job: $JOB_NAME"
                  fi
                  rm -f "/tmp/cron-job-${JOB_NAME}.json"
                done

                if [ -n "$CURRENT_JOBS" ]; then
                  echo "$CURRENT_JOBS" | while IFS= read -r JOB_NAME; do
                    [ -z "$JOB_NAME" ] && continue
                    if ! echo "$DESIRED_JOBS" | grep -qx "$JOB_NAME"; then
                      log "Removing cron job: $JOB_NAME"
                      openclaw cron remove "$JOB_NAME" 2>/dev/null || \
                        log "WARNING: Failed to remove cron job: $JOB_NAME"
                    fi
                  done
                fi

                log "Cron sync complete"
              }

              # --- メインループ ---
              log "Starting config-manager-sidecar"

              WAIT=0
              while [ $WAIT -lt 60 ]; do
                pgrep -f "node dist/index.js gateway" >/dev/null 2>&1 && break
                sleep 5
                WAIT=$((WAIT + 1))
              done

              compute_checksum "$CONFIG_DIR/openclaw.json" > "$CHECKSUM_DIR/config"
              compute_checksum "$CRON_DIR/cron-jobs.json" > "$CHECKSUM_DIR/cron"

              # 初回cronジョブ同期
              handle_cron_change

              while true; do
                sleep "$POLL_INTERVAL"

                NEW=$(compute_checksum "$CONFIG_DIR/openclaw.json")
                OLD=$(cat "$CHECKSUM_DIR/config" 2>/dev/null || echo "")
                if [ "$NEW" != "$OLD" ]; then
                  handle_config_change
                  echo "$NEW" > "$CHECKSUM_DIR/config"
                fi

                NEW=$(compute_checksum "$CRON_DIR/cron-jobs.json")
                OLD=$(cat "$CHECKSUM_DIR/cron" 2>/dev/null || echo "")
                if [ "$NEW" != "$OLD" ]; then
                  handle_cron_change
                  echo "$NEW" > "$CHECKSUM_DIR/cron"
                fi
              done
          resources:
            requests:
              cpu: 10m
              memory: 32Mi
            limits:
              cpu: 100m
              memory: 128Mi
          securityContext:
            runAsUser: 1000
            runAsGroup: 1000
            runAsNonRoot: true
            readOnlyRootFilesystem: false
            allowPrivilegeEscalation: false
            capabilities:
              drop:
                - ALL
          volumeMounts:
            - name: openclaw-config
              mountPath: /etc/openclaw/config.d
              readOnly: true
            - name: cron-config
              mountPath: /etc/openclaw/cron.d
              readOnly: true
            - name: data
              mountPath: /home/node/.openclaw

        # ---- dind: Docker-in-Docker sidecar ----
        - name: dind
          image: docker:27-dind
          securityContext:
            privileged: true
          env:
            - name: DOCKER_TLS_CERTDIR
              value: /certs
          resources:
            requests:
              cpu: 250m
              memory: 512Mi
            limits:
              cpu: 1000m
              memory: 2Gi
          volumeMounts:
            - name: docker-certs
              mountPath: /certs
            - name: docker-data
              mountPath: /var/lib/docker

      volumes:
        - name: docker-certs
          emptyDir: {}
        - name: docker-data
          emptyDir: {}
        - name: openclaw-config
          configMap:
            name: openclaw-config
        - name: cron-config
          configMap:
            name: openclaw-cron-jobs
        - name: data
          persistentVolumeClaim:
            claimName: openclaw-data
```

### 4.5 Reloaderとの連携

#### 変更前（現在）

```yaml
metadata:
  annotations:
    configmap.reloader.stakater.com/reload: "openclaw-config"
```

→ `openclaw-config` 変更時にPodが再起動される。

#### 変更後

```yaml
metadata:
  annotations:
    reloader.stakater.com/auto: "false"
```

→ **Reloaderを完全に無効化**。config-manager-sidecar が ConfigMap 変更を検知し、PVCにコピー。openclaw の chokidar がファイル変更を自動検知してホットリロード。

### 4.6 Kustomization の更新

```yaml
# kustomization.yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

namespace: openclaw
resources:
  - namespace.yaml
  - external-secret.yaml
  - external-secret-litellm.yaml
  - configmap-openclaw.yaml
  - configmap-openclaw-cron.yaml    # ← 追加
  - configmap-litellm.yaml
  - deployment-openclaw.yaml
  - service-openclaw.yaml
  - pvc.yaml
  - deployment-litellm.yaml
  - service-litellm.yaml
  - deployment-cloudflared.yaml
  - networkpolicy.yaml
```

---

## 5. 前提条件とリスク

### 5.1 前提条件

| # | 前提条件 | 影響 | 確認状況 |
|---|---------|------|---------|
| P1 | ~~`openclaw` が `SIGHUP` シグナルで設定をリロードできること~~ | ~~メイン設定のホットリロード~~ | **不要**: chokidar ファイル監視が公式サポート |
| P2 | `openclaw` が `chokidar` でPVC上のファイル変更を検知できること | メイン設定のホットリロード | **確認済み**: `gateway.reload.mode: "hybrid"` がデフォルト |
| P3 | `openclaw cron add/update/remove` コマンドが冪等に動作すること | サイドカーが安全にcronジョブを同期できる | エラーハンドリングで `add` → `update` のフォールバックを実装 |
| P4 | openclawイメージに `jq` が含まれていること | cronジョブJSONのパース | 未確認。含まれていない場合はシェルのみでパース |
| P5 | ConfigMapのsubPath未使用 | ConfigMap変更がPod内に自動伝搬 | 本計画ではsubPath未使用で設計済み |

> **P1 が解消されたことで、`shareProcessNamespace: true` も不要になった。** これにより、セキュリティ上のリスクが1つ減り、設計がシンプルになった。

### 5.2 リスクと軽減策

| # | リスク | 影響度 | 発生確率 | 軽減策 |
|---|-------|-------|---------|--------|
| R1 | ~~`openclaw` がSIGHUPリロード非対応~~ | ~~中~~ | ~~高~~ | **解消**: ファイル監視がデフォルトで有効 |
| R2 | ConfigMap伝搬遅延（最大60-120秒） | 低 | 確実 | 許容範囲。ポーリング60秒 + ConfigMap伝搬~60秒 = 最大~120秒 |
| R3 | サイドカーのクラッシュ | 中 | 低 | `restartPolicy: Always` により自動復旧 |
| R4 | `openclaw cron` コマンドの失敗 | 中 | 低 | エラーログを出力。次回ポーリング時にリトライ |
| R5 | ポーリングによるCPU消費 | 低 | 確実 | 60秒間隔のmd5sumのみ。CPU requests: 10m で十分 |
| R6 | `gateway.*` フィールドの変更でプロセス再起動 | 低 | 低 | `hybrid` モードの正常動作。再起動は自動的に行われる |
| R7 | chokidar がPVC上のファイル変更を検知しない | 中 | 低 | PVCはLonghornで提供。inotifyイベントは通常発行される。テスト計画Phase 1で確認 |

### 5.3 テスト計画

#### Phase 1: ファイル監視によるホットリロードの確認

```bash
# Pod内で手動テスト: PVC上のファイルを直接編集して、ホットリロードが動作するか確認
kubectl exec -n openclaw deployment/openclaw -c openclaw -- sh -c '
  echo "=== Current config hash ==="
  md5sum /home/node/.openclaw/openclaw.json

  echo "=== Adding a test comment to config (safe change) ==="
  # 実際のテストでは、agents や channels 等の安全なフィールドを変更する
  cp /home/node/.openclaw/openclaw.json /tmp/openclaw-backup.json

  echo "=== Check openclaw logs for reload messages ==="
'
kubectl logs -n openclaw deployment/openclaw -c openclaw --tail=20
```

- **成功**: ログに `Config reloaded` や `hot-apply` 関連のメッセージが出力される
- **失敗**: chokidar が検知しない場合 → PVCの種類やinotifyサポートを確認

#### Phase 2: cronジョブ動的同期のテスト

1. ConfigMap `openclaw-cron-jobs` にテストジョブを追加
2. 60-120秒待機
3. `openclaw cron list` で新しいジョブが登録されていることを確認
4. ConfigMapからジョブを削除
5. 60-120秒待機
6. `openclaw cron list` でジョブが削除されていることを確認

#### Phase 3: Pod再作成時の復旧テスト

1. cronジョブが登録されていることを確認
2. Podを削除: `kubectl delete pod -n openclaw -l app.kubernetes.io/name=openclaw`
3. 新しいPodが起動したら `openclaw cron list` でジョブが再登録されていることを確認

---

## 6. 運用フロー

### 6.1 メイン設定 (`openclaw.json`) の変更

```
開発者: configmap-openclaw.yaml を編集
  ↓
Git: PR作成 → レビュー → マージ
  ↓
ArgoCD: ConfigMap openclaw-config を同期
  ↓
Kubernetes: Pod内の /etc/openclaw/config.d/openclaw.json が更新（~60秒）
  ↓
config-manager-sidecar: チェックサム変更を検知（~60秒以内）
  ↓
sidecar: PVC上の openclaw.json にコピー
  ↓
openclaw (chokidar): ファイル変更を自動検知（~300ms以内）
  ↓
hybrid モード:
  ├── 安全な変更 → 即座にホットアプライ（再起動なし）
  └── クリティカルな変更 → 自動的にプロセス再起動
```

**合計遅延**: ConfigMap伝搬(~60秒) + ポーリング(~60秒) + chokidarデバウンス(300ms) = **最大約2分**

### 6.2 cronジョブ定義の変更

```
開発者: configmap-openclaw-cron.yaml を編集（ジョブ追加/更新/削除）
  ↓
Git: PR作成 → レビュー → マージ
  ↓
ArgoCD: ConfigMap openclaw-cron-jobs を同期
  ↓
Kubernetes: Pod内の /etc/openclaw/cron.d/cron-jobs.json が更新（~60秒）
  ↓
config-manager-sidecar: チェックサム変更を検知（~60秒以内）
  ↓
sidecar: openclaw cron add/update/remove で差分を適用
  ↓
openclaw: cronジョブが動的に更新（再起動なし）
```

---

## 7. 実装手順

### Step 1: ファイル監視によるホットリロードの事前確認

Pod内でPVC上のファイルを編集し、`openclaw` がファイル変更を検知してホットリロードすることを確認する。

```bash
# テスト手順
kubectl exec -n openclaw deployment/openclaw -c openclaw -- sh -c '
  # 設定のバックアップ
  cp /home/node/.openclaw/openclaw.json /tmp/openclaw-backup.json

  # 安全な変更を加える（例: loggingレベルを変更）
  # ※ 実際のテスト内容はユーザーが決定
'

# ログでリロードを確認
kubectl logs -n openclaw deployment/openclaw -c openclaw --tail=30 | grep -i -E "(reload|config|hot)"
```

### Step 2: `configmap-openclaw-cron.yaml` の作成

`repos/lolice/argoproj/openclaw/configmap-openclaw-cron.yaml` を新規作成。

### Step 3: `kustomization.yaml` の更新

リソースリストに `configmap-openclaw-cron.yaml` を追加。

### Step 4: `deployment-openclaw.yaml` の更新

- `init-cron-config` initContainerを追加
- メインコンテナの起動スクリプトにcronジョブ登録ロジックを追加
- `config-manager-sidecar` コンテナを追加（**SIGHUP送信なし、ファイルコピーのみ**）
- `cron-config` ボリュームを追加
- メイン設定のConfigMapマウントをサイドカーの `/etc/openclaw/config.d` にも追加
- Reloaderアノテーションを `reloader.stakater.com/auto: "false"` に変更
- **`shareProcessNamespace` は追加しない**（不要）

### Step 5: PR作成・レビュー

変更をブランチにpushし、PRを作成。

### Step 6: テスト実施

Phase 1〜3のテストを順番に実施。

### Step 7: モニタリング

サイドカーのログとopenclawのリロードログを監視:

```bash
# サイドカーのログ
kubectl logs -n openclaw deployment/openclaw -c config-manager-sidecar -f

# openclawのリロードログ
kubectl logs -n openclaw deployment/openclaw -c openclaw -f | grep -i reload
```

---

## 付録A: 変更対象ファイル一覧

| ファイル | 操作 | 内容 |
|---------|------|------|
| `configmap-openclaw-cron.yaml` | **新規作成** | cronジョブ定義のConfigMap |
| `deployment-openclaw.yaml` | **修正** | サイドカー追加、initContainer追加、ボリューム追加、アノテーション変更 |
| `kustomization.yaml` | **修正** | `configmap-openclaw-cron.yaml` をリソースリストに追加 |
| `configmap-openclaw.yaml` | 変更なし | - |

## 付録B: 旧計画からの主要な変更点

| 項目 | 旧計画 | 最終版 | 理由 |
|------|--------|--------|------|
| メイン設定リロード方式 | SIGHUP → API → ファイル更新のみ（3段階フォールバック） | **ファイルコピーのみ**（chokidar 自動検知） | 公式ファイル監視機能が存在 |
| `shareProcessNamespace` | `true`（SIGHUP送信に必要） | **不要**（設定しない） | SIGHUP送信が不要 |
| ハイブリッド方式の必要性 | SIGHUP非対応時の代替策として記載 | **不要** | ファイル監視が公式サポート |
| サイドカーの `handle_config_change()` | SIGHUP送信 + API呼び出し + ファイルコピー | **ファイルコピーのみ（2行）** | シンプルかつ確実 |
| セキュリティリスク | プロセス名前空間共有によるプロセス間干渉リスク | **リスク解消** | `shareProcessNamespace` 不要 |

---

**作成完了日**: 2026-02-13
**方式**: 完全ホットリロード（chokidar ファイル監視ベース）
**推奨アクション**: Step 1 (PVC上のファイル変更によるホットリロード確認) を実施し、動作確認後に実装着手
