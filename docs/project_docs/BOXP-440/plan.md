# BOXP-440: 読取専用 Langfuse MCP

## 目的と設計

Langfuse にはサーバー側の読取専用モードも読取専用 API キーもないため、プロジェクトキーを MCP クライアントへ渡さず、プロキシがメソッドとツールを許可リストで制限する。Secret はプロキシ Pod にファイルとしてマウントし、リクエスト時に読み取る。上流の Host 検査には公開ホスト `langfuse.b0xp.io` を指定する。クライアントの認証ヘッダーは使用しない。

許可ツールは `listObservations`, `getObservation`, `getObservationFieldSchema`, `getObservationFilterSchema`, `getObservationFilterValues`, `getHealth`。一覧にはさらに上流が `readOnlyHint: true` とするものだけを表示する。`listObservations` の limit は最大 50。リクエストは 64 KiB、レスポンスは 512 KiB が上限。ストリーミングや batch は扱わない。

Calico は codex-workspace と hermes-agent の指定ラベルからプロキシの TCP 3002 への接続を許可する。プロキシからの外向き通信は既存の DNS と Langfuse namespace 内へのルールに限る。

## クライアント登録

```sh
hermes mcp add langfuse --url http://langfuse-mcp-readonly.langfuse.svc.cluster.local:3002/mcp
claude mcp add --scope user --transport http langfuse http://langfuse-mcp-readonly.langfuse.svc.cluster.local:3002/mcp
codex mcp add langfuse --url http://langfuse-mcp-readonly.langfuse.svc.cluster.local:3002/mcp
```

## 自己観測と限界

Task Board exporter の本文対象ツール集合と Hermes observer には MCP Langfuse ツールを含めず、出力本文は記録しない。許可された観測データはクライアントに返るため、アクセス可能な Pod とログの扱いには注意する。上流のツール名やスキーマ、readOnlyHint が変わった場合は一覧や呼び出しの互換性を再検証する。プロキシはユーザー認証の代わりに NetworkPolicy に依存する。

## ロールバック

クライアント登録を削除し、プロキシの YAML、ConfigMap generator と NetworkPolicy の追加ルールを削除する。
