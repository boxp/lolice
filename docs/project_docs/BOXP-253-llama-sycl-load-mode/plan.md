# BOXP-253: local-llm の mmap 無効化指定を llama.cpp 新旧 revision 両対応の環境変数に移す

チケット: BOXP-253（local-llm Gemma4 追加高速化）。設計・測定の正本は Obsidian `Projects/local-llm-gemma4-optimization`。
対になる変更: boxp/arch PR #13802（`llama-sycl` image を llama.cpp v0.7.0 + oneDNN に更新）。

## 背景

- arch PR #13802 の候補 image（llama.cpp `757cad02` = v0.7.0）を golyat-4 で隔離計測したところ、同一 model / 量子化 / ctx 262144 / f16 KV / `-ub 2048` で
  decode 11.6 → 13.5 t/s、pp512 104 → 126 t/s、pp2048 140 → 250 t/s の改善を確認した（品質 probe は正本を参照）。
- 一方 v0.7.0 では `--no-mmap` が削除され `--load-mode none` に置き換わった。現 Deployment の args は `--no-mmap` を渡しているため、
  image だけが `latest` 経由で更新されると llama-server が `invalid argument: --no-mmap` で起動できず crashloop になる。
- `latest` の更新は argocd-image-updater が自動で lolice に書き戻すため、arch 側 merge の前に lolice 側を新旧どちらの revision でも起動できる形にしておく必要がある。

## 変更

- `argoproj/local-llm/deployment.yaml`: args から `--no-mmap` を外し、環境変数 `LLAMA_ARG_MMAP=0`（旧 revision が読む）と
  `LLAMA_ARG_LOAD_MODE=none`（v0.7.0 が読む）を追加する。未知の環境変数はどちらの revision も無視するため、image 更新の前後どちらでも
  従来と同じ「mmap を使わず RAM に読み込む」挙動になる。環境変数は router が起動する model 子プロセスにも継承される。
- model / 量子化 / ctx / batch / preset（`models.ini`）/ Envoy は変更しない。

## 反映手順

1. この PR を merge → Pod が Recreate で再起動（現 image のまま。`/models` の `gemma4-26b` preset に `mmap = 0` 相当が残ることを確認）。
2. arch PR #13802 を merge → main ビルドで `latest` 更新 → image-updater が digest を lolice に書き戻し → Pod 再起動。
3. live の image digest / `/models` の args / health を確認し、Service 経由で同条件（pp512/tg128/pp2048/tg32@2048、8K/16K 長文、品質 probe）の after 計測を行い正本に記録する。
4. 退行時は arch 側で `LLAMA_REF` を戻す（この PR の環境変数は旧 revision でも有効なので lolice 側の rollback は不要）。
