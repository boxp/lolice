# cloudflared の live digest と実 binary 版数

取得日: 2026-10-07 UTC。Docker Hub 公開 registry から対象 digest を取得し、manifest 参照先の全 layer digest を検証しました。amd64 バイナリだけを `/tmp` に展開し、各 binary は `--version` のみ実行しました。Pod exec と cluster 変更は行っていません。

| live workload | live imageID digest | registry manifest 解決 | amd64 binary `--version` |
|---|---|---|---|
| `argocd/cloudflared-api` | `sha256:072c067d25ccbe61d46e18f0d0723255f2bb5304f7317caa95b27031520ff92c` | manifest list/index。amd64 child `sha256:2fa795d0271a71c133a8f17c19a2a625e1976ad716329e0e61789f9fd8ee0091` | `cloudflared version 2026.9.3 (built 2026-09-24-16:19 UTC)` |
| `boxp-home/cloudflared` | `sha256:b392761b711c0e5649d9b64e1fc9a10ba0563fa3e712ed7c26bde5cc1fbe9059` | amd64 Docker manifest（config metadata: linux/amd64） | `cloudflared version 2026.7.3 (built 2026-07-23-10:24 UTC)` |

`072c...` は manifest index として digest 直接取得できました。index の amd64 child から binary を確認しています。取得時点の `latest` tag は別 index (`sha256:9b49eed8...`) を指していたため、移動する tag の表示ではなく live imageID digest を基準に対応付けました。

registry 取得物、temporary layer、抽出 binary は確認後に削除し、sanitized evidence のみ残しています。
