# Kohako Studio (BOXP-441)

Cloudflare Access の Owner と codex-workspace の専用 audience の投影 token のみを受け付けます。Hermes API key は ESO で単一 Secret から取得し、STT は設定しません。Access application 作成後、Deployment の AUD placeholder を実値に置換してからマージします。イメージは main ビルドの latest を Pod 起動時に取得します。
