# ADR (Architecture Decision Records)

この repo で後の変更が守るべき決定と、その理由を残します。
検査の仕組みは [.regomark/README.md](../../.regomark/README.md)、書き方の指示は [.claude/rules/adr.md](../../.claude/rules/adr.md) にあります。
新しい ADR は [template.md](template.md) をコピーして作ります。

## 書くもの

- 後の変更が守るべき決定と、その理由と帰結
- repo の実装に紐づき、公開または共有してよい確定判断

## 書かないもの

- 実装の詳細、定数、手順書。これらは source や別の docs に置き、ADR からは相対リンクで示します
- 検討中の案、private な判断、作業ログ、秘密情報、個人環境の絶対パス

草案は未 merge の PR や repo 外のノートで扱います。Proposed という Status は使いません。
merge された ADR は決定済みの記録です。

## 形式

- ファイル名は `NNN-kebab-case.md` です。番号は 001 から欠番なく振り、000 は使いません
- H1 は `ADR-NNN: タイトル` で、番号はファイル名と揃えます
- `## Status` `## Context` `## Decision` `## Consequences` をこの順に 1 個ずつ置きます。本文は日本語で書きます
- Status の先頭段落は次のいずれかだけにします
  - `Accepted`: 有効な決定
  - `Rejected`: 採用しなかった提案
  - `Deprecated`: 無効になり、置き換え先のない決定
  - `Superseded by ADR-NNN`: 後の ADR に全体を置き換えられた決定
- 一部だけ置き換えられた場合は、先頭段落の次の行に `Partly replaced by ADR-NNN` を書きます
- Accepted の ADR にある repo 内リンクは実在する必要があります

## 決定を変えるとき

Accepted の決定を書き換えず、新しい ADR を追加して古い ADR の Status を更新します。
