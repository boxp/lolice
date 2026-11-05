# BOXP-417: 現ハードの律速究明と高性能ローカルモデルの高速化

チケット: BOXP-417
正本ノート: Obsidian `Projects/strata-localllm-evaluation/performance-frontier-request.md`、`performance-frontier-ledger.md`

## 目的

Strata / llama.cpp に限定せず、golyat-4（Core Ultra 9 285H、Arc 140T、DDR5-5600 2ch）で高性能モデルの速度を決めている律速を計測で絞り、品質を落とさない改善策を試す。恒久採用や既定変更は含まない。

## このリポジトリで行う変更

研究の間だけ次の診断モードにする。研究の終了時または長期中断時に別の復帰 PR で戻す。

- `argoproj/local-llm/deployment.yaml`: replicas 0
- `argoproj/strata-eval/manifests/deployment.yaml`:
  - replicas 1、command `sleep infinity`、memory limit 76GiB（#1037 と同じ）
  - `/m`: `/var/lib/local-llm/models` の読み取り専用 mount（QCN など既存 weights を複製せず使う）
  - `/w`: `/var/lib/local-llm/models/boxp417` の書き込み mount（測定記録と隔離 build。外部公開なし）

## 手順

1. 受付 close → drain → scale 0 → GPU とメモリの解放を確認 → この PR を merge。
2. 仮説ごとに有限の batch を実行する。予測、棄却条件、上限は ledger に開始前に記録する。
3. 終了時は復帰 PR → strata pod 消滅 → scale 1 → 受付を閉じたまま Gemma 実推論 → QCN 設定不変の確認 → 受付 open。

## 境界

権限の追加、driver / host kernel の変更、node reboot、電力上限の撤去、大規模 weights の取得、恒久的な既定変更はしない。健康停止は MemAvailable < 12GiB が 2 連続、GPU HANG / reset の増加。
