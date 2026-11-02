# BOXP-379: localllm 創作既定を Gemma4 26B へ戻す（coder の Qwen3-Coder-Next は維持）

## 背景

BOXP-367/#965 で創作既定を `ornith15-35b-a3b` に変更したが、Owner が Gemma4 26B へ戻すと明示した。Novel Board は `CODEX_NOVEL_BOARD_LOCALLLM_MODEL=gemma4-26b` で既に Gemma を使用しており、runner のコード上の既定値も `gemma4-26b` である。

## 変更内容

- task-board-runner の `CODEX_TASK_BOARD_LOCALLLM_MODEL` の値１行を `gemma4-26b` に変更する。
- `gemma4-26b` preset 自体は変更しない。モデルは `Gemma4-26B-A4B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M.gguf`、ctx は 262144、router は `--models-max 1` である。
- coder の環境変数と preset は変更しない。Ornith および他の候補 preset・weights は削除せず、MTP など不採用の最適化は再有効化しない。

## 反映手順

PR → argocd-diff CI → codex review → squash merge → ArgoCD auto sync の順に進める。Recreate rollout では task-board runner を prepare-shutdown で計画停止し、再開後に環境変数を readback する。

## 確認

- 再開後に環境変数を readback する。
- 非 canon の小さな日本語 fixture 票を通常の Board 経路（assignee `localllm`）で１回生成し、同一ノートを短く改稿する。metadata で `model=gemma4-26b`、fallback なし、所要時間を確認する。
- coder の `CODER_MODEL` と router preset が変更前後で不変であることを readback する。

## ロールバック

value を `ornith15-35b-a3b` に戻す PR を作成する。
