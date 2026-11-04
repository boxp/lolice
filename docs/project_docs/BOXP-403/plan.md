# BOXP-403: Strata と現行 coder を難しい coding 課題で比較する

チケット: BOXP-403（BOXP-383 の追加実験）
正本ノート: Obsidian `Projects/strata-localllm-evaluation/hard-coding-owner-summary.md`、`hard-coding-comparison-request.md`

## 目的

現行 Qwen coder（QCN）に対して、Flash-Next の IQ3_XXS と高精度 UD-Q4_K_XL を使う実益があるかを、修正の正しさとやり直しを含む総所要時間で確認する。恒久採用や専用運用への切替は含まない。

## このリポジトリで行う変更

試験中だけ次の診断モードにし、終了後に別の復帰 PR で戻す（#1027、#1028 と同じ差分）。

- `argoproj/local-llm/deployment.yaml`: replicas 0
- `argoproj/strata-eval/manifests/deployment.yaml`: replicas 1、command `sleep infinity`、memory limit 76GiB

## 手順

1. QCN の 4 試行は受付 open の通常経路で実行する。この PR は merge しない。
2. 受付 close → drain → scale 0 → GPU とメモリの解放を確認 → この PR を merge。
3. 診断 pod で IQ3_XXS と UD-Q4 の 8 試行を実行する（順序は固定、再試行なし）。
4. 復帰 PR → strata pod 消滅 → scale 1 → 受付を閉じたまま Gemma 実推論 → QCN 設定不変を確認 → 受付 open。

## 固定した条件

- 課題: 公開可能な合成 fixture 2 件（複数ファイルの機能追加、原因特定と回帰 test）。各構成 2 回、計 12 試行。
- 1 試行の予算: 1200 秒、assistant turn 60、tool 80、edit + write 30、出力 16000 token。超過と失敗は分母に残す。
- 全体の上限: QCN 開始から 6 時間（見積もり GPU 約 90 分、最悪 約 4.5 時間）。
- 健康停止: MemAvailable < 12 GiB が 2 連続、GPU HANG / reset の増加。

## 含まないもの

恒久採用、既定 model の変更、追加 DL、削除、node / driver の再起動、権限の追加、比較 patch の本番適用。
