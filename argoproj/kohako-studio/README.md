# Kohako Studio (BOXP-441)

Cloudflare Access の Owner と codex-workspace の専用 audience の投影 token のみを受け付けます。Hermes API key は ESO で単一 Secret から取得し、STT は設定しません。Access AUD は arch の kohako_studio Access application(arch#14517)の値です。イメージは main ビルドの latest を Pod 起動時に取得します。

## 可用性（BOXP-511）

web は 2 replicas、BFF は PVC と単一 writer のため 1 replica に分離する。旧 Deployment の prune に伴う PVC 再接続時は一度 API が停止する。ロールバックはこの変更を revert する。
