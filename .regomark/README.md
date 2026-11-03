# ADR の構造検査と Rego lint

[regomark](https://github.com/babarot/regomark) で `docs/adr/` を検査します。ADR の書き方は [docs/adr/README.md](../docs/adr/README.md) にあります。

## 実行入口

```sh
.regomark/check.sh          # 同梱内容の検証、opa check/fmt/test、Regal、ADR 検査、e2e
.regomark/check.sh --docs   # ADR 検査だけ
.regomark/check.sh --judge  # ADR 検査に加えて LLM の意味検査 (opt-in)
```

## 終了コード

`regomark check` の終了コードをそのまま返します。

| code | 意味 |
| --- | --- |
| 0 | deny なし。warn は 0 のまま |
| 1 | deny あり |
| 2 | 設定、解析、policy の異常で検査できない |

## 構造検査 (deny)

| rule | 検査内容 |
| --- | --- |
| `file-name` | ファイル名が `NNN-kebab-case.md` |
| `title` | H1 が 1 個で `ADR-NNN: タイトル`、番号がファイル名と一致 |
| `required-sections` | Status/Context/Decision/Consequences が 1 個ずつある |
| `section-order` | 上記の順序 |
| `status-paragraph` | Status 直下が段落 |
| `status-value` | Accepted/Rejected/Deprecated/Superseded by ADR-NNN |
| `status-lines` | 続く行は `Partly replaced by ADR-NNN` だけ |
| `reference-placement` | 置換参照は Status の先頭段落だけ |
| `reference-target` | 置換先が実在 |
| `reference-direction` | 置換先は自分より後の番号 |
| `replacement-cycle` | 置換が循環しない |
| `duplicate-number` | 番号が重複しない |
| `number-sequence` | 001 から欠番なし、000 なし |
| `broken-link` | Accepted の repo 内リンクが実在 |

ファイル改名でもリンクが壊れるため、CI は ADR の変更 path に絞らず全 PR で実行します。

## 意味検査 (warn)

`spec-detail`、`procedure`、`multiple-decisions` を LLM に尋ねます。回答は warn だけが読むため、deny と終了コードには影響しません。

- 必須 CI は常に `--no-judge` 相当で動きます。LLM の未実行は通過を意味しません
- LLM に渡すのは Accepted の ADR の Decision と Consequences だけです
- `--tools ""` で LLM に repo を変更する手段を与えません
- 回答は質問の内容から作ったキーに紐づくため、ADR や policy が変わると古い回答は無効になります
- 上限は `.regomark.yaml` の `judge.max_questions` です。超えると `--judge` は exit 2 で止まります

## 3 repo で揃える方法

正本は boxp/arch です。`.regomark/shared.sha256` に列挙したファイルは 3 repo で同一に保ちます。
正本を変更したら `.regomark/check.sh --update-manifest` で一覧を更新し、同じ内容を各 repo へ PR で反映します。
`check.sh` は同梱内容と一覧の一致を必須で検証し、boxp/arch の main との差は warning で知らせます。

ツールの版は repo ごとの方式で固定し、Renovate が追跡します。

- boxp/arch: `aqua/imports/` (checksum は `aqua/aqua-checksums.json`)
- boxp/lolice、boxp/even-g2-lab: `.regomark/tools.env` (SHA-256 を併記)

`tools.env` を更新する Renovate PR は checksum が合わないため `.regomark/install-tools.sh --refresh` で更新してから merge します。

## ライセンス

policy と e2e はこの repo 用に書いたものです。regomark 本体は MIT です。
記事が参照する babarot/oksskolten の policy は AGPL-3.0、babarot/claude-recall はライセンス表記がないため、コードを流用せず、記事と regomark README が示す規則を基に実装しています。
