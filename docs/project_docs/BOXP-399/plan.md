# BOXP-399 再発防止計画

- dotfiles と arch の helper で root による書き込みを拒否し、回帰テストと実行手順を追加する。
- Hermes の bootstrap で共有 tmp 上の lock ディレクトリを 1000:10000、2775 で作成する。
- 各 PR を検証し、arch と lolice は再起動に関する Owner の了承までマージしない。runtime helper の更新と Pod 再作成後の検証は別途実施する。
