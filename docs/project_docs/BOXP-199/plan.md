# BOXP-199 実施計画

## 目的と境界

Hermes gatewayをKubernetesで動かし、Discordの専用非公開voice channelを音声クライアントとして使えるか確認する。Desktopおよびhermes-live-voiceのDesktop/Codex app-server経路は採用しない。本番接続、常時接続、本番Secret投入は別の明示的Go/No-Goが必要。

## 手順

1. 指定worktree、Notes、配備の宣言とruntimeメタデータを確認する。
2. 公式Hermesのrevisionを固定し、Discord platform plugin、音声依存、認証、承認、記録経路を調べる。
3. Discordの既存application/guild/channelは値を保存せず棚卸しする。取得不能な項目は未確認とする。
4. 本番と分離したstaging設計、追加guardの要件、最小権限の作成手順、検証・rollback手順をPRにする。
5. 安全guardと専用test channelが揃った場合だけ非機密定型文で実音声検証を行う。
6. focused validationと独立レビューを行い、実行済みと未実行を区別して報告する。

## 現時点の判断

2026-09-30 UTC: runtimeのpods/execとSecret一覧がRBACで拒否された。Discord管理セッション・専用test channel・テスト参加者は確認できていない。現行gatewayのPVCには他用途の状態があるため音声実験には共有しない。未検証の音声adapterを自動同期対象へ追加せず、設計のみのDraft PRで引き継ぐ。追加guardの実装とstaging検証は未完了であり、音声接続はNo-Go。

## 成果物

- [設計・運用手順](design.md)
- [確認結果と受入条件の状況](verification.md)
