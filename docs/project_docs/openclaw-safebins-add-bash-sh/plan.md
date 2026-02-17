# OpenClaw safeBins に bash/sh を追加

## チケット
T-20260217-003

## 背景・原因

OpenClaw の `tools.exec.security=allowlist` + `ask=off` 環境において、エージェントが `gh` 等のコマンドを実行する際に `exec denied: allowlist miss` エラーが発生する。

### 根本原因
OpenClaw の実行基盤は、コマンドを直接実行するのではなく `/bin/sh -lc "<command>"` や `bash -c "<command>"` 経由で実行する。`security=allowlist` モードでは、実行されるバイナリ名が `safeBins` に含まれていない場合、`exec denied: allowlist miss` として拒否される。

現在の `safeBins` には `gh`, `ls`, `cat` 等の個別コマンドは含まれているが、それらを起動するシェルインタープリタ自体（`bash`, `sh`）が含まれていないため、シェル経由の実行がすべてブロックされていた。

## 対応内容

### 変更ファイル
- `argoproj/openclaw/configmap-openclaw.yaml`
  - `tools.exec.safeBins` 配列に `"bash"` と `"sh"` を追加

### 変更差分
```diff
 "safeBins": [
+  "bash", "sh",
   "gh", "ghq", "gwq",
   "ls", "cat", "grep",
```

## gh ラッパーへの影響

`bash`/`sh` を safeBins に追加しても、`gh` コマンドのサブコマンド制限（`configmap-gh-wrapper.yaml`）は維持される。理由:

1. `pathPrepend: ["/opt/gh-wrapper"]` により、`gh` の実体は常にラッパースクリプトが優先される
2. `bash -c "gh pr merge"` のようなシェル経由の呼び出しでも、子プロセスの `PATH` にはラッパーが先頭に配置されるため、ラッパーのポリシーが適用される
3. `/usr/bin/gh` の直接パス指定は、そもそも `safeBins` にパス付きバイナリが含まれないため `allowlist miss` で拒否される

## リスク評価

- **低リスク**: `bash`/`sh` はシェルインタープリタであり、他のコマンドを呼び出す手段だが、呼び出し先のコマンドも同様に `safeBins` チェックを受ける（allowlist モードのセキュリティは維持）
- `ask=off` との組み合わせでも、実行可能なコマンドは `safeBins` に列挙されたもののみに限定される
