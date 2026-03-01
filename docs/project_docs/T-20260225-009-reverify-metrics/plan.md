# T-20260225-009: OpenClaw メトリクス再検証結果

## 概要

PR #7123（`boxp/arch`: OTel 依存追加）反映後に `openclaw_*` メトリクスの取得状況を再検証した。

## 検証結果サマリ

| 項目 | 前回 (2026-02-21) | 今回 (2026-02-25) | 変化 |
|------|------------------|------------------|------|
| `@opentelemetry/api` エラー | 発生 | **消失** | 改善 |
| プラグインロード失敗ログ | 発生 | **消失** | 変化 |
| `openclaw_*` メトリクス | 0/18 | **0/18** | 変化なし |
| `target_info` (OTel自動生成) | なし | なし | 変化なし |

**結論: PR #7123 により `@opentelemetry/api` のモジュール解決エラーは解消されたが、メトリクスは依然として Prometheus に到達していない（0/18）。**

## 新たな根本原因仮説

1. **SDK 内部初期化失敗（最有力）**: OTel パッケージが `/opt/otel-deps/node_modules` にインストールされ `NODE_PATH` で参照可能になったが、SDK 初期化時にバージョン競合や内部エラーで静かに失敗
2. **OTLP エクスポーター送信失敗**: OTel SDK のデフォルト動作で export エラーが swallow されている
3. **OpenClaw v2026.2.22 でのプラグインシステム変更**: `[plugins]` ログが一切なく、プラグインロード方式が変更された可能性

## 推奨アクション

1. **即時**: ConfigMap に `OTEL_LOG_LEVEL=debug` を追加して OTel 内部エラーを特定
2. **短期**: OpenClaw v2026.2.23 へアップデート（changelog 確認後）
3. **代替**: `/app/node_modules` 直接インストール方式への変更を検討

## 詳細

検証の完全な結果は以下を参照:
- `docs/project_docs/openclaw-app-metrics-dashboard/metrics-verification.md`
