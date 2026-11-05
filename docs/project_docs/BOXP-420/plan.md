# BOXP-420: G2 の安全な curl・Web 検索で許可画面を省略する

## 目的

Kohako G2 session で、読み取りと証明できる curl と Web 検索だけ許可画面を省略する。それ以外は既存の確認を維持する。Telegram の許可運用は変えない。

## 変更

- `kohako-g2-context` gate（1.5.0）: curl を allowlist で解析し、通れば公式 `modify` で検証済みの形へ書き換えて実行する。`web_search` は検索語を検査して通す。
- `platform_toolsets.api_server` に `search`（`web_search` のみ）を追加する。`web_extract` は出さない。
- bootstrap: PVC の旧管理値を新しい値へ書き換える。

## 反映

merge すると Argo CD の自動 sync で Hermes Pod が再起動する。再起動は Owner の別確認を得てから行う。

## 確認

plugin の unittest、mutation、上流 `url_safety.py` との照合、bootstrap 移行試験、`kustomize build`、反映後の G2 実経路。
