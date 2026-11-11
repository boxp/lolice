# BOXP-503 実施計画

API server の toolset はプラットフォーム共通で、現状の G2 向け設定では kohako-home Owner も Telegram 相当のツールを使えない。API server の設定を拡張し、pre_llm_call で Web のターンと委任元を記録、pre_tool_call で Owner のテキストターンとその委任子のみ許可する。Hermes 本来の危険コマンド・書き込み承認は維持する。

G2 の既存 allowlist と承認、candidate・verifier・その他 API セッションの全拒否、ふれあい・不明ターンの全拒否は維持する。clarify、tts、computer_use は API server で表示できず、image_gen は新たな課金を避けるため除外する。

G2 に渡るツール schema は増えるが、ゲートは従来通り拒否する。Web の cronjob や delegation は Telegram 相当の権限を持つため、実運用で動作と承認を確認する。子セッションの許可は記録された直近の親ターンに依存する。

検証: unit test と bootstrap Python の構文・設定移行を確認する。デプロイ後に config と /v1/toolsets を読み戻し、実ブラウザで verifier が拒否されること、Owner の実ツール呼び出し、承認の once/deny/expiry/replay/stop、ふれあいターンでツールを使わないことを確認する。

ロールバック: PR を revert するだけでは旧 bootstrap は新リストを管理値と認識せず WARNING を出して残す。revert に加え、旧 bootstrap の管理対象に BOXP-503 の新リストを含める変更を適用し、設定が旧値へ戻ることを確認する。
