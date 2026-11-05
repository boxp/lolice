# BOXP-407 配備設定の計画

- even-g2-lab のアプリ側上限なし対応を先にCI・P0/P1レビュー後にmergeし、image配信を確認する。
- BFF の KOHAKO_REALTIME_MAX_MS を明示的な unlimited にする。旧imageでは解釈できないため、このPRを先にmergeしない。
- 変更は even-g2-main の通常web/BFFに限定し、Hermes/codex-workspace の変更・再起動は行わない。
- 同時接続1、開始毎時6回、入力途絶20秒、認証・承認・dedupを維持する。
- kustomize出力、通常CIとP0/P1レビューを確認し、配備後はimage/digest/環境設定をreadbackする。
- 公開routeはAccess403、内部routeはtimeout、Pod exec/portforwardは権限なし。実合成通話が確認できない場合は成功とせず未確認区間とbuildを記す。
