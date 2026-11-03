# ADR-001: GitOps の決定を ADR に残し regomark と Rego で構造を必須検査する

## Status

Accepted

## Context

クラスターの構成判断の理由は、これまで repo 内にまとまった形で残っていなかった。
文書だけのルールは守られたかを機械的に確かめられず、決定の記録とコードのリンクが改名で壊れても気づけない。

Owner は 2026-11 に、この repo へ ADR と regomark、Rego lint を導入すると決めた。
構造は決定的に検査できるが、ADR が仕様書や手順書になっていないかは LLM でしか判定できず、結果も実行ごとに揺れる。

## Decision

後の変更が守るべき決定で、公開してよいものは `docs/adr/` に ADR として残す。
検討中の案、private な判断、作業ログは ADR に移さない。この repo は public である。

ADR の構造違反は regomark の deny とし、全 PR で必須 check の `status-check` に集約して merge を止める。
ADR の変更 path に絞らないのは、他のファイルの改名でも ADR のリンクが壊れるからである。

Status に Proposed は使わない。草案は未 merge の PR か repo 外のノートで扱う。

LLM の意味検査は warn だけとし、必須 check の成否に影響させない。実行は利用者の明示操作に限り、未実行を通過として扱わない。

policy は boxp/arch を正本として同じ内容を保つ。

却下した案:

- 全文書を Obsidian に残す案: repo の変更と決定を同じ PR で検査できない
- conftest で検査する案: Markdown を読めず、別途パーサーが必要になる
- 新しい GitHub App で policy を配布する案: 新しい権限を増やす費用に見合わない

## Consequences

検査の入口と規則は [.regomark/README.md](../../.regomark/README.md)、policy は [.regomark/policy](../../.regomark/policy)、必須 check への統合は [chainsaw.yaml](../../.github/workflows/chainsaw.yaml) にある。

ADR が指すファイルを改名または削除する PR は、ADR のリンクも直さないと merge できない。

policy を変更するときは、[.regomark/shared.sha256](../../.regomark/shared.sha256) を正本と揃える必要がある。

[.regomark/policy](../../.regomark/policy) の検査対象は ADR だけで、既存の [policy](../../policy) には適用しない。
