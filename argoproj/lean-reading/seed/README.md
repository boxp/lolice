# 読書実習用ワークスペース（コンパイラ証明技法）

この画面はブラウザで動く VS Code（code-server）です。ファイルは本人専用の保存領域に残り、
タブを閉じても、サーバーが再起動しても消えません。

## 置き場所

| フォルダ | 中身 | 使い方 |
| --- | --- | --- |
| `practice/` | 練習用。本を読みながら自分で書く場所 | 章ごとに `Practice/Ch02.lean` のようなファイルを作り、`Practice.lean` に `import Practice.Ch02` を足す |
| `lean-verified-compiler/` | 本のサポートサイトのコード（完成版） | 読む・動かして確かめる参照用。答え合わせ用なので、先に見たくないときは開かない |
| `practice/Practice/Basics.lean` | InfoView の見方の確認用 | 本とは無関係の小さな例 |

Lean のバージョンは本と同じ `leanprover/lean4:v4.35.0`（`lean-toolchain` に記載）。

## InfoView（型・評価結果・証明途中のゴール）

1. `.lean` ファイルを開く。
2. カーソルを `#check` / `#eval` の行、または `by` の下の行に置く。
3. InfoView に型、評価結果、その時点のゴールが出る。
   - InfoView が無いとき: 画面上部の `∀` メニュー → "Toggle InfoView"。
   - InfoView はエディタの下に開く（スマホの縦画面でも読める幅にするため）。右に置きたいときは
     設定（Ctrl+,）で "Open Side By Side Direction" を "right" にする。
   - スマホ: 最初に左端のファイルのアイコン（Explorer）を押して一覧を閉じると、エディタと
     InfoView が画面いっぱいに出る。ファイルを開くときはもう一度押す。
   - AI チャット（Copilot）の欄は使わないので消してある。

## ターミナル（メニュー → Terminal → New Terminal）

```sh
cd ~/workspace/practice && lake build          # 練習用を build
cd ~/workspace/lean-verified-compiler && lake build && lake exe lean-verified-compiler
```

このターミナルの中には秘密情報や Obsidian の vault はありません。外部への通信は HTTPS（443）だけです。

## 保存と復元

- 保存: 自動保存（少し間を置いて保存）が有効。手で保存するときは Ctrl+S。
- 手元に控える: エクスプローラでフォルダを右クリック → "Download..."。
- 戻したいとき: `practice/` で `git init` して、区切りごとに `git commit` しておくと
  `git log` / `git checkout <commit> -- <file>` で戻せる。
- このファイルや `Practice/Basics.lean` を消すと、次の再起動で元の内容が戻る（あるファイルは上書きしない）。
- 本のコードを最新にしたいとき: `cd ~/workspace/lean-verified-compiler && git pull`。
