# Babashka Integration Plan for OpenClaw

## Overview

OpenClaw環境内でBabashka (bb) を実行可能にするための計画書。
変更対象は `boxp/arch` (Dockerfile / Ansibleロール) がメイン。`boxp/lolice` は計画書配置のみ（マニフェスト変更なし。イメージタグ更新は別PR）。

## 1. Babashka について

Babashka はClojure互換のスクリプティングツール。GraalVM Native Imageでビルドされた単一の静的バイナリとして配布され、JVMの起動オーバーヘッドなしにClojureスクリプトを実行可能。

- **公式リポジトリ**: https://github.com/babashka/babashka
- **インストール方法**: GitHubリリースから静的バイナリをダウンロード
- **サポートプラットフォーム**: Linux (amd64, aarch64), macOS, Windows
- **バイナリサイズ**: 約30MB (解凍後、静的リンク版)

## 2. 変更計画

### 2.1. `boxp/arch` - Ansibleロール (物理ノード向け)

**目的**: Orange Pi Zero 3 (ARM64) クラスターノードにBabashkaをインストールするAnsibleロールを作成。

**ロール名**: `babashka`

**ディレクトリ構造**:
```
ansible/roles/babashka/
├── meta/main.yml           # Galaxy metadata
├── defaults/main.yml       # デフォルト変数 (バージョン等)
├── tasks/main.yml          # インストールタスク
├── molecule/
│   └── default/
│       ├── molecule.yml    # Moleculeテスト設定
│       ├── converge.yml    # テスト実行プレイブック
│       ├── prepare.yml     # テスト準備
│       └── verify.yml      # 検証タスク
└── README.md               # ドキュメント
```

**インストール手順** (tasks/main.yml):
1. 必要パッケージ(curl)の確認・インストール
2. Babashkaバイナリのダウンロード (GitHub Releases)
3. SHA256チェックサムの検証
4. 解凍して `/usr/local/bin/bb` に配置
5. 実行権限の設定
6. バージョン確認

**デフォルト変数** (defaults/main.yml):
- `babashka_version`: "1.12.214" (最新安定版、2025-12-22リリース)
- `babashka_install_dir`: "/usr/local/bin"
- `babashka_arch_map`: `ansible_architecture` factに基づくアーキテクチャマッピング (`x86_64` → `amd64`, `aarch64` → `aarch64`)
- `babashka_checksum`: SHA256チェックサム値 (アーキテクチャ別)
  - amd64: `2926098700f6e230b21007871b47844280d29e641959b693535a5d74e4dab4a3`
  - aarch64: `b6bc6d28a41cb303b429cdbd565311a046719c842dcb25e8ad1ed2929f9145fe`

**プレイブックへの組み込み**:
`ansible/playbooks/control-plane.yml` の `roles:` セクションに babashka ロールを追加:
```yaml
roles:
  - role: user_management
    ...
  - role: network_configuration
    ...
  - role: kubernetes_components
    ...
  - role: babashka
    tags: [babashka, tools]
```

### 2.2. `boxp/arch` - Dockerfile修正 (OpenClawコンテナイメージ)

**目的**: OpenClawコンテナイメージにBabashkaを組み込む。

**方法**: Dockerfileのマルチステージビルドで、Babashkaバイナリをダウンロードし、SHA256チェックサムを検証して最終イメージにコピー。

**変更内容** (`docker/openclaw/Dockerfile`):
```dockerfile
# 新しいビルドステージを追加（digest固定で再現性確保）
FROM debian:bookworm-slim@sha256:98f4b71de414932439ac6ac690d7060df1f27161073c5036a7553723881bffbe AS babashka-download
ARG BABASHKA_VERSION=1.12.214
ARG BABASHKA_SHA256=2926098700f6e230b21007871b47844280d29e641959b693535a5d74e4dab4a3
SHELL ["/bin/bash", "-euo", "pipefail", "-c"]
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && \
    curl -fSL -o babashka.tar.gz \
      "https://github.com/babashka/babashka/releases/download/v${BABASHKA_VERSION}/babashka-${BABASHKA_VERSION}-linux-amd64-static.tar.gz" && \
    echo "${BABASHKA_SHA256}  babashka.tar.gz" | sha256sum -c - && \
    tar xzf babashka.tar.gz && \
    chmod +x bb

# 最終ステージに追加
COPY --from=babashka-download /bb /usr/local/bin/bb
```

**理由**:
- マルチステージビルドにより最終イメージサイズへの影響を最小限に（バイナリのみコピー）
- SHA256チェックサムでバイナリの整合性を検証（fail-closed: 不一致時はビルド失敗）
- `curl -fSL` でHTTPエラー時に即座に失敗
- `SHELL ["/bin/bash", "-euo", "pipefail", "-c"]` でパイプ内エラーも検出
- ベースイメージをdigest固定でビルド再現性を確保
- OpenClawコンテナは `linux/amd64` で動作 (`nodeSelector: kubernetes.io/arch: amd64`)
- 静的バイナリのため追加の依存関係不要

### 2.3. `boxp/lolice` - Kubernetesマニフェスト確認

**結論**: lolice側のK8sマニフェスト変更は不要。

Babashkaは `boxp/arch` のDockerfileでイメージに組み込まれ、`/usr/local/bin/bb` にインストールされる。
現在のPATH設定 (deployment-openclaw.yaml:148) に `/usr/local/bin` が含まれているため、追加の環境変数変更は不要。

```yaml
- name: PATH
  value: "/shared-bin:/shared-npm/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
```

**注意**: `boxp/arch` 側でDockerイメージがビルド・プッシュされた後、`deployment-openclaw.yaml` のイメージタグ更新は別途のPRで対応する（本計画の範囲外）。

## 3. テスト計画

### 3.1. Ansibleロール テスト (Molecule)

- **ローカルテスト** (x86_64): `cd ansible/roles/babashka && molecule test`
- **ARM64テスト**: `MOLECULE_DOCKER_PLATFORM=linux/arm64 molecule test`
- **正常系検証項目**:
  - `bb` バイナリが `/usr/local/bin/bb` に存在する
  - 実行権限が設定されている
  - `bb --version` が正常に実行できる
  - SHA256チェックサムが検証されている
  - 既にインストール済みの場合はスキップされる (冪等性)
- **異常系検証項目**:
  - チェックサム不一致時にタスクが確実に失敗する
  - 未対応アーキテクチャ指定時に明示的エラーで停止する

### 3.2. Dockerイメージ テスト

- **ローカルビルド**: `docker build -t openclaw-test docker/openclaw/`
- **正常系検証項目**:
  - `docker run openclaw-test bb --version` で正常にバージョンが出力される
  - `docker run openclaw-test which bb` で `/usr/local/bin/bb` が返る
  - SHA256チェックサムがビルド中に検証されている
  - 既存のツール (gh, ghq, gwq, codex, claude) に影響がない
- **回帰テスト項目**:
  - OpenClawの起動が正常に行われる
  - 既存CLIツール (`gh`, `ghq`, `gwq`, `codex`, `claude`) が引き続き動作する

### 3.3. デプロイ後テスト

- ArgoCD同期後、OpenClaw Pod内で `bb --version` を実行
- 簡単なBabashkaスクリプト実行テスト: `bb -e '(println "Hello from Babashka!")'`
- OpenClawの主要機能（gateway起動、ヘルスチェック）が正常に動作することを確認

### 3.4. CI自動ゲート条件

- Molecule テスト: PR マージの必須条件
- Docker ビルド成功: イメージプッシュの必須条件
- チェックサム検証失敗時: ビルド即時停止（fail-closed）

## 4. セキュリティ考慮事項

### 4.1. バイナリの信頼性と供給チェーンリスク
- Babashkaは公式GitHubリリースからダウンロード
- **SHA256チェックサム検証を初期実装に含める** (DockerfileおよびAnsibleロールの両方)
- **チェックサム値の管理方針**:
  - チェックサム値はGitHub Releases公式ページの `.sha256` ファイルから取得
  - 取得したチェックサム値はコードレビュー済みのPRでバージョン管理される
  - バージョン更新時はRenovateまたは手動PRで、チェックサム値の同時更新をレビュー必須とする
  - チェックサム不一致時はビルド/インストールを即座に中止（fail-closed）
- **残余リスク**: GitHub自体の侵害やリリースパイプラインの侵害は完全には防げない。SHA256検証はダウンロード時の改ざん・破損を検出するが、ビルドインフラの侵害には対応できない。リスクは許容範囲と判断する

### 4.2. 実行権限
- Babashkaは `node` ユーザー (UID 1000) として実行
- 特権昇格不要 (`allowPrivilegeEscalation: false` 維持)
- コンテナの `capabilities: drop: [ALL]` セキュリティコンテキストと互換

### 4.3. ネットワークアクセス
- 既存のNetworkPolicyでBabashkaのネットワークアクセスも制限される
- 外部HTTPアクセスはRFC1918除外ルールに従う

### 4.4. ファイルシステム
- `/usr/local/bin/bb` はイメージビルド時に含まれるため読み取り専用
- 実行時の一時ファイルは既存のPVCマウントポイントで管理

## 5. ロールバック戦略

### 5.1. 即時復旧（第一選択）
1. `lolice` リポジトリで `deployment-openclaw.yaml` のイメージタグをArgoCDで現在稼働中の直前安定タグに戻すPRを作成・マージ
2. ArgoCDが自動同期し、Babashka非搭載の安定イメージにロールバック
3. 復旧後に原因調査を実施

### 5.2. Dockerfile変更のロールバック
1. PRをリバートするか、Babashka関連の行を削除
2. イメージを再ビルド・プッシュ
3. `lolice` リポジトリで `deployment-openclaw.yaml` のイメージタグを新タグに更新するPRを作成・マージ

### 5.3. Ansibleロールのロールバック
1. ロールの適用をスキップするか、PRをリバート
2. 手動でノードから `/usr/local/bin/bb` を削除: `ansible control_plane -m file -a "path=/usr/local/bin/bb state=absent"`

### 5.4. ロールバック判定基準
- OpenClaw Podが起動失敗（CrashLoopBackOff）する場合
- 既存CLIツール（gh, ghq, gwq, codex, claude）が動作しなくなった場合
- コンテナのリソース使用量が異常に増加した場合

### 5.5. 緊急時
- Babashkaは独立した静的バイナリのため、削除しても他のコンポーネントに影響なし
- OpenClawの動作に必須ではないため、削除による障害リスクなし

## 6. 実装順序

1. `boxp/arch`: Babashka Ansibleロールの作成 + control-plane.ymlへの組み込み
2. `boxp/arch`: Dockerfileの修正 (SHA256検証付き)
3. `boxp/arch`: PR作成
4. `boxp/lolice`: 計画書ドキュメントの配置 + PR作成
5. （別途）`boxp/arch` のイメージビルド完了後、`boxp/lolice` でイメージタグ更新PRを作成

## 7. 影響範囲

| コンポーネント | 影響 | リスク |
|---|---|---|
| OpenClawコンテナイメージ | バイナリ追加 (~30MB) | 低 - イメージサイズ増加のみ |
| Orange Pi Zero 3 ノード | Ansibleロール追加 | 低 - 新規バイナリの配置のみ |
| control-plane.yml | babashkaロール追加 | 低 - 既存ロールに影響なし |
| NetworkPolicy | 変更なし | なし |
| PVC | 変更なし | なし |
| 既存ツール | 影響なし | なし |
