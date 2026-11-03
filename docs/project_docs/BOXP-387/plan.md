# BOXP-387: Kohako G2 リアルタイム音声対話の Hermes 側（mint plugin）と BFF 設定

設計全体は boxp/even-g2-lab の `docs/project_docs/BOXP-387/plan.md` を正とする。ここでは lolice 側の変更だけを書く。

## 変更

1. `argoproj/hermes-agent/plugins/kohako-g2-realtime/`: Hermes API server に
   `POST /v1/kohako/realtime/client-secrets` を追加する user plugin。
   - `plugin.yaml` の `requires_auth: [openai-codex]` により、Hermes core の公式 API
     `credentialed_provider_request("openai-codex", ...)` で `api.openai.com/v1/realtime/client_secrets` を
     署名付きで呼ぶ。token は Hermes 内に留まり、plugin は auth ファイルを読まず refresh もしない。
     API キー経路は無い（sign-in が無ければ 503）。
   - 認証は API server 自身の Bearer 検査（`adapter._check_auth`）。body は
     `{sessionId, instructions, voice, model}` のみ、model/voice は allowlist、instructions は 2000 字まで、毎時 12 回。
   - 応答は `{value, expiresAt, model}` だけ。log は status と所要 ms だけで secret / instructions / upstream 本文は出さない。
2. `kustomization.yaml` の configMapGenerator、`deployment.yaml` の bootstrap（copy・enable・mount・volume）、
   `configmap.yaml` の `plugins.enabled`（既存の BOXP-332 パターン）。
3. `argoproj/even-g2-lab/deployment.yaml`: kohako-bff に `KOHAKO_REALTIME=1`、
   `KOHAKO_REALTIME_ORIGIN=https://kohako-g2.b0xp.io`、`KOHAKO_REALTIME_MAX_MS=120000`。
   BFF の egress は公開 443 を既に許可しているため NetworkPolicy の変更は無い。

## 影響と承認

- この PR の merge で Argo CD が hermes-agent Deployment を sync し、**Hermes Pod が再作成される**（Recreate）。
  Owner の明示承認が要る（BOXP-387 の境界: Hermes 再起動は未承認）。
- even-g2-lab 側の BFF 実装（同 ticket の PR）が main に入ってから有効化すること。BFF は `KOHAKO_REALTIME=1` で
  plugin に到達できなければ 502/504 を返し、通常の会話・音声入力には影響しない。

## 検証

- `python3 -B -m unittest argoproj/hermes-agent/plugins/kohako-g2-realtime/test_kohako_g2_realtime.py`
- `kubectl kustomize argoproj/hermes-agent` / `argoproj/even-g2-lab` が通ること。
- 前回 run で同じ mint 経路を Hermes Pod 上の一時 plugin から実行し、200 と合成音声の往復を確認済み。
