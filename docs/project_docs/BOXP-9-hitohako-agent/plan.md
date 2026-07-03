# BOXP-9: hitohako-agent on Even G2

## Goal

Even G2 の PC-98 風クライアントから、lolice 上の `hitohako-agent` backend に問い合わせ、Obsidian Headless で同期された Vault を参照しながら短い応答を返せる MVP にする。

## Architecture

```text
Even G2 app
  -> VITE_HITOHAKO_AGENT_ENDPOINT /v1/chat
  -> codex-workspace Pod sidecar: hitohako-agent
  -> shared /home/boxp PVC
  -> /home/boxp/Documents/obsidian-headless/BOXP
  -> obsidian-sync sidecar
```

## Implementation Plan

- `boxp/even-g2-lab`
  - `apps/hitohako-agent` を追加し、`POST /v1/chat` と `GET /healthz` を提供する。
  - backend は Vault の `キャラ設定/ひとはこ.md`、当日 daily note、`ひとはこ/*.md` を read し、G2 向け短文応答を生成する。
  - 会話ログは `Captures/hitohako-agent/YYYY-MM-DD.md` への append に限定する。
  - G2 client は `VITE_HITOHAKO_AGENT_MODE=api` のときだけ backend API を使い、未設定時は明示 mock のままにする。
- `boxp/lolice`
  - `codex-workspace` Pod に `hitohako-agent` sidecar を追加し、既存 home PVC と Obsidian Headless sidecar を共有する。
  - Service と NetworkPolicy に `hitohako-agent` port 8080 を追加する。
  - API key は既存 ExternalSecret `codex-workspace-gemini` から注入し、Git には置かない。

## Validation

- `boxp/even-g2-lab`: `npm test`, `npm run typecheck`
- `boxp/lolice`: `kubectl kustomize argoproj/codex-workspace`

## Follow-up

- `hitohako-agent-main` image の publish workflow と ECR repository の存在確認。
- 実機 G2 では画像更新頻度を抑え、backend 応答表示中の口パクは「発言中だけ開口」程度に留める。
