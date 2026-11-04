# BOXP-406: Strata 難課題比較の時間上限を延ばした追加比較

チケット: BOXP-406（BOXP-403 の追加実験）
正本ノート: Obsidian `Projects/strata-localllm-evaluation/extended-coding-plan.md`、`extended-coding-request.md`

## 目的

BOXP-403 では 1 試行 20 分の上限で 13 回中 11 回が打ち切られ、最後まで完了できるかと総時間を比較できなかった。上限を延ばして、3 構成の完了率と完了までの時間を観測する。恒久採用や専用運用への切替は含まない。

## このリポジトリで行う変更

試験中だけ次の診断モードにし、終了後に別の復帰 PR で戻す（#1031、#1033 と同じ差分）。

- `argoproj/local-llm/deployment.yaml`: replicas 0
- `argoproj/strata-eval/manifests/deployment.yaml`: replicas 1、command `sleep infinity`、memory limit 76GiB

## 手順

1. QCN の 4 試行は受付 open の通常経路で実行する。この PR は merge しない。
2. 受付 close → drain → scale 0 → GPU とメモリの解放を確認 → この PR を merge。
3. 診断 pod で IQ3_XXS と UD-Q4 の 8 試行を実行する（順序は固定、再試行と置換なし）。
4. 復帰 PR → strata pod 消滅 → scale 1 → 受付を閉じたまま Gemma 実推論 → QCN 設定不変を確認 → 受付 open。

## 固定した条件

- 課題: BOXP-403 と同じ公開可能な合成 fixture 2 件。各構成 2 回、計 12 試行。
- 1 試行の予算: 3000 秒、assistant turn 200、tool 300、edit + write 120、出力 60000 token。超過と失敗は分母に残す。
- context: agent 側 32768、圧縮は reserve 8192、keepRecent 12000 を全構成で同じにする。出力上限は 1 要求 4096。
- 受付停止の上限: close から 8 時間。試行は close から 7 時間 35 分までに終わるものだけ開始する。
- 全体の上限: QCN 開始から 12 時間。
- 健康停止: MemAvailable < 12 GiB が 2 連続、GPU HANG / reset の増加。
