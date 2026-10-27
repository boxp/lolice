# BOXP-328: Kohako G2 の HTTPS 入口・WARP 本人認証（S1）

設計正本: Obsidian `Projects/kohako-even-g2/architecture.md`（BOXP-324）§4-4・§4-6・§11・§12 の Slice 1。Owner 承認済み（2026-10-27）。

## 変更（3 repo）

| repo | 変更 |
| --- | --- |
| boxp/even-g2-lab | nginx に Kohako 専用 listener `:8081`（`/kohako/` のみ、`/kohako/api/probe` は固定 200、他は 404、`nosniff`・`Referrer-Policy`・最小 CSP）。既存 `:8080` の `/kohako/api/` は 404。診断欄に `origin` / `top-level`。`npm run qr:kohako-g2:https`。nginx route 契約テストを CI に追加 |
| boxp/lolice | Service `kohako-g2-web`（ClusterIP 非固定、80 → nginx `:8081`）、Pod の port 8081、Calico で cloudflared → 8081 のみ許可 |
| boxp/arch | even-g2-lab module：proxied CNAME `kohako-g2.b0xp.io` → 既存 tunnel、ingress（`^/kohako/` → `kohako-g2-web`、`originRequest.access` で JWT 必須・aud 固定）、Access アプリ（One Client 認証 ON、Binding Cookie OFF、24h）、policy（Owner email ∧ Require Gateway の 1 本、他は既定拒否）、posture rule `Require Gateway`。k8s module：既定 WARP profile の Include に host `kohako-g2.b0xp.io` を 1 件追加 |

## 境界

- 証明書：Cloudflare edge（Universal SSL `*.b0xp.io`、既存）。cert-manager・新 token なし。
- origin 迂回防止：DNS は Access アプリと tunnel ingress の後に作成（`depends_on`）。cloudflared が Access JWT（署名・issuer=team・aud）を検証。`kohako-g2-web` は WARP の private route 対象外で、private L4 経路（`even-g2-main` 固定 ClusterIP → :8080）からは `:8081` に届かない。
- Require Gateway だけでは本人扱いしない（email 必須）。login_method は登録 IdP 未確定のため require しない。
- 変更しない：`even-g2-main.b0xp.io`（Hitohako・受入済み FIXTURE）、他 Access アプリ、organization の全アプリ適用、`warp_auth_non_browser_401`、PR #19 / BOXP-323 hold、BFF・Hermes・Board・音声（FIXTURE 維持）。

## 順序

1. even-g2-lab PR merge → image build → Image Updater で配備（`:8081` 有効化）
2. lolice PR merge → Argo sync（Service / NetworkPolicy）
3. arch PR plan 確認 → merge → apply → readback（DNS / Access / tunnel / Include）
4. 未認証 curl が edge で拒否（origin に届かない）、private 経路の `/kohako/api/probe` が 404 を確認
5. Owner 実機確認（QR・初回 / 再起動後 / WARP OFF 拒否）

## rollback（fail-closed の順）

1. Access policy を外す（アプリは残し既定拒否）または DNS レコード削除 = arch の revert。Access だけ外して origin を公開しない（cloudflared の JWT 必須も残る）。
2. tunnel ingress・posture・Include の host を削除（arch revert）。
3. lolice の Service / NetworkPolicy、even-g2-lab の :8081 を revert（到達経路が無いので無害）。
