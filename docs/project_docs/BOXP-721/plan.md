# BOXP-721: Hermes・taskrunner の通信監査（観測専用）— 計画

元計画: BOXP-631（Obsidian `Projects/agent-prompt-injection-defense/plan.md`）の A1・A3 だけを log-only で入れる。

## 範囲

- 入れる: agent 専用 DNS resolver（A1）、Calico `action: Log`（A3）、Alloy での収集と DNS 名の伏字、停止確認用の Alloy カウンタ。
- 入れない: 通信の遮断（LAN 拒否・allowlist enforce）、RBAC/権限の変更、dind 撤去、秘密の移動、TLS 復号、新しい課金、BOXP-632 の是正、Alertmanager への新しい通知。

## 手順

1. 現行仕様を Obsidian `Projects/agent-egress-audit/spec.md` に書く。
2. Alloy パイプラインのテスト（`tests/agent-egress-audit/test_alloy_pipeline.sh`、本物の Alloy v1.7.0 で実行）を仕様の例から作り、失敗を確認してから実装する。
3. PR1: resolver・Alloy・NetworkPolicy（Log と resolver 宛て許可）・Hermes の dnsConfig。merge 後に反映を確認する。
4. 記録試験: 管理下の `b0xp.io` の存在しないサブドメイン（canary、秘密を含まない）を問い合わせ、Loki に伏字済みで出ること、接続記録が出ることを確かめる。
5. PR2: codex-workspace の dnsConfig（taskrunner の Pod が再作成されるため、run の終わりに merge する）。
6. 初回実観測（宛先の上位・量・欠測）を Obsidian の判断入口へ記す。後続の検知・通知案（TD-02）を示す。

## Lean を使わない理由

状態機械の規則ではなく、設定と 1 入力 1 出力の文字列変換（伏字）なので、仕様の例から作った直接テストで確かめる。
