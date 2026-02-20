# SLO検証手順: board.md 更新反映

## SLO目標

| 優先度 | 目標 | 測定区間 |
|--------|------|----------|
| 最悪ケース | <= 10秒 | board.mdファイル変更 → ブラウザ表示反映 |

## メカニズム

1. OpenClaw が PVC 上の `board.md` を更新
2. nginx サイドカーが `open_file_cache off` で常にディスクから直読み
3. フロントエンドが 5秒間隔で `/api/board.md` を fetch ポーリング
4. `Cache-Control: no-store` により Cloudflare Edge / ブラウザキャッシュを抑止

理論的最大遅延: 5秒（ポーリング間隔） + ネットワーク遅延 = 約 1〜6秒

## 検証手順

### 前提条件

- `board.b0xp.io` にブラウザからアクセス可能な状態であること
- Cloudflare Access でGitHub認証済みであること
- OpenClaw Pod が Running 状態であること

### 手順1: 初期表示確認

1. ブラウザで `https://board.b0xp.io` を開く
2. Task Board のMarkdown内容がHTML描画されていることを確認
3. ヘッダーの "Updated: HH:MM:SS" が表示されていることを確認

### 手順2: 更新反映時間の計測

1. ストップウォッチを準備する
2. OpenClaw のコンテナ内で board.md に変更を加える:
   ```bash
   kubectl exec -n openclaw deployment/openclaw -c openclaw -- \
     sh -c 'echo "<!-- SLO test: $(date -Iseconds) -->" >> /home/node/.openclaw/workspace/tasks/board.md'
   ```
3. 変更コマンド実行直後にストップウォッチ開始
4. ブラウザ上で変更が反映されるまでの時間を計測
5. 更新タイムスタンプ ("Updated: HH:MM:SS") が更新されたことを確認

### 手順3: 結果判定

- 反映時間 <= 10秒: **PASS**
- 反映時間 > 10秒: **FAIL** — 以下を確認:
  - nginx `open_file_cache off` が設定されているか
  - `Cache-Control` ヘッダーが正しく付与されているか (`curl -I`)
  - Cloudflare Edge キャッシュが効いていないか

### 手順4: HTTPヘッダー確認

```bash
curl -sI https://board.b0xp.io/api/board.md | grep -i cache
```

期待される出力:
```
Cache-Control: no-store, no-cache, must-revalidate
Pragma: no-cache
CDN-Cache-Control: no-store
```

### 手順5: CSPヘッダー確認

```bash
curl -sI https://board.b0xp.io/ | grep -i content-security-policy
```

期待される出力:
```
Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self'; img-src https: data:; font-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'
```

### 手順6: クリーンアップ

```bash
kubectl exec -n openclaw deployment/openclaw -c openclaw -- \
  sh -c "sed -i '/<!-- SLO test:/d' /home/node/.openclaw/workspace/tasks/board.md"
```

## 繰り返し検証

上記手順2-3を3回以上繰り返し、全試行で <= 10秒を満たすことを確認する。
