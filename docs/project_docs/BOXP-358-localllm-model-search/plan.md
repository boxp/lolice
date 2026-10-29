# BOXP-358: lolice の local LLM 候補の継続比較

## 目的

lolice 上で実用最高性能の local LLM を継続的に比較する。品質、速度、資源の実測を残し、採用や既定モデルの切替は比較後に別途判断する。

## 変更内容

- この PR は `qwen36-35b-a3b` と `ornith15-35b-a3b` の選択可能な preset だけを追加する。
- 既定モデルと Deployment は変更しない。router の `--models-max 1` により、候補は名前を指定した要求でのみロードされる。
- モデルの取得先は `/var/lib/local-llm/models/boxp358/` とし、固定 revision と sha256 を照合する。
- sampler は呼び出し側が指定する。context などは router の設定を使う。

## 資源と時間の上限

- 候補 1 つの weights + KV + buffer は 55 GB 以下とする。
- `MemAvailable` が 12 GiB を下回ったら候補を unload する。
- disk は 800 GB を確保し、本票のダウンロードは合計 150 GB までとする。
- 1 候補の試験は 90 分を上限とする。

## 試験の手順

1. 候補を取得し、revision と sha256 を確認する。
2. router 経由で BOXP-337 と同じ suite（load / F / L32 / B253 / A1 / A2）を実行する。
3. 品質、速度、資源の結果を記録し、候補を逐次比較する。
4. 試験後は既定モデルを再ロードし、正常な復帰を確認する。

## rollback

この PR を revert して preset を削除する。取得済みの `/models/boxp358/<dir>` は Owner の判断後に削除する。
