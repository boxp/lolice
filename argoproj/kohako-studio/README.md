# Kohako Studio (BOXP-441)

Cloudflare Access の Owner と codex-workspace の専用 audience の投影 token のみを受け付けます。Hermes API key は ESO で単一 Secret から取得し、STT は設定しません。TTS は同 namespace の AivisSpeech Engine（CPU、`kohako-studio-tts`、声はコハク／ノーマル、ACML 1.0）を BFF からのみ呼びます（BOXP-522）。Access AUD は arch の kohako_studio Access application(arch#14517)の値です。イメージは main ビルドの latest を Pod 起動時に取得します。

## 可用性（BOXP-511）

web は 2 replicas、BFF は PVC と単一 writer のため 1 replica に分離する。旧 Deployment の prune に伴う PVC 再接続時は一度 API が停止する。ロールバックはこの変更を revert する。

## モデルの有限試行（BOXP-706 M1）

BFF の `KOHAKO_MODEL_TRIAL` で、本人の家の会話の run にだけ `openai-codex` / `gpt-6.2-sol` を指定する（30 発言、または 2026-11-27T19:00:00Z の早い方。指定を付けた発言が 2 回続けて失敗しても終了）。終わると何も指定しなくなり、Hermes の既定のモデルへ戻る。Hermes の設定・Telegram・Webhook・cron は変えない。回数は PVC の `/data/model-trial.json` に残る。途中で止める・終了後に片づける時は、この env を消す（`deployment-bff.yaml` の該当ブロックを revert）。期限や回数を延ばす・範囲を広げるには Owner の新しい選択が要る。仕様は vault の `Projects/kohako-home/model-trial-spec.md`。
