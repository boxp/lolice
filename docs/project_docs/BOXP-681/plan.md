# BOXP-681: Hermes v0.23.3 で webhook の配送要確認 notice が消える回帰への対処

## 背景
- boxp/arch#14496 (Hermes v0.23.1→v0.23.3) の image build は arch#15554 で解消済み。
- notify-lab stage2 を同条件で比較したところ、v0.23.3 では T3（Notes 印なしの無通知）で
  gateway が `silence marker kept silent: warning notifications are suppressed for webhook` を出し、
  boxp-notify に何も渡らず「配送要確認」notice が届かない。v0.23.1 は `silence marker rejected` で notice を配送。
- 原因: 上流 v0.23.3 の `gateway/run_config_loaders.py::_silence_allowed_for_source` が、
  `allow_silence` 未設定かつ `display.platforms.<p>.suppress_warning_notifications: true`（BOXP-554 で本番が設定）の時、
  印のみの最終応答を無通知扱いにする。明示 `allow_silence`（true/false）があれば従来判定。

## 変更
- `argoproj/hermes-agent/deployment.yaml` の bootstrap に、config.yaml の `platforms.webhook.allow_silence: false` を
  冪等に追記するブロック（PYSILENCE）を追加。既存の「channel に allow_silence は与えない」規則の明示化で、本体 patch なし。
  - 本番形（`platforms.webhook.enabled: true`）→ `allow_silence: false` を1行追加
  - `platforms` 無し → 末尾に block 追加 / `webhook` 無し → `platforms` 直下に追加
  - 手編集の値・flow 形式など想定外の形 → WARNING を出し書き換えない
- WEBHOOK_ENABLED の env 有効化は yaml に `enabled` が無い/true の時は維持される（上流 config_env.py）。

## 検証
- 形状別（本番形・lab形・webhook無し・手編集・flow形）を候補 image の python で実行し、期待どおり・2回目は no-op。
- notify-lab stage2（ローカル, `--network none`）: v0.23.1 / v0.23.3 / v0.23.3+本修正 を比較。
  本修正で T3/T5 が v0.23.1 と同じ判定に戻り、判定一覧は v0.23.1 と一致。
- A1 以降は relay の静音時間（02:00-09:00 JST）で非 critical の resolved が `[SILENT]` になるため、
  実行時刻依存で v0.23.1 でも停止。日中に再実行して全判定を確認する。

## 手順
1. 本 PR を merge → ArgoCD 反映、Pod 起動ログで `configured webhook allow_silence false` と Hermes 起動を確認。
2. 日中に notify-lab を v0.23.3 候補で全判定確認 → arch#14496 を merge → image 反映・起動/会話/通知確認。

Spec/Lean: 設定移行の1関数で直接の形状別テストで足り、状態機械の規則は無いため Lean は不要。
