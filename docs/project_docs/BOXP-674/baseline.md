# 変更前のCI測定基点

9fef0b6e5946f624ff42deb6f3527ceae564a1f4 は origin/main 9f6611b0 の動作を変えず、stage2.sh にコメントだけを加えたコミット。同一PRイベント・同一対象pathで全checkを起動し、後続実装と比較する。

- PR: https://github.com/boxp/lolice/pull/1365
- Hermes notify lab: https://github.com/boxp/lolice/actions/runs/45150366974
- Manifest Validation: https://github.com/boxp/lolice/actions/runs/45150367187
- gitleaks: https://github.com/boxp/lolice/actions/runs/45150367109
- mainでの変更前追加測定: https://github.com/boxp/lolice/actions/runs/45150105435

計測はworkflowの起動から最後のjob完了まで（全workflowの最早created_at〜最遅completed_at）、job本体、隔離stage2本体を分ける。image digest、arch checkout SHA、runner OSを照合し、異なるものは同条件と呼ばない。
