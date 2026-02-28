# StarRupture Dedicated Server

StarRupture ゲーム専用サーバーの Kubernetes マニフェスト。

## 運用上の注意

### Manage Server（Web UI）は使用しないこと

StarRupture の Manage Server（ブラウザ経由のサーバー管理 UI）は **使用禁止** とする。

**理由:**
Manage Server は UE Remote Control Plugin 経由で `DedicatedServerSettingsComp` にアクセスするが、
このコンポーネントの初期化がセーブデータの状態に依存しており、
初期化失敗時に `DedicatedServerSettingsComp does not exist` エラーが発生する。
この状態では「ゲームをスタート」ボタンが無反応になり、復旧が困難になる。

**標準運用:**
- サーバー設定は **DSSettings.txt** で管理する（`USE_DSSETTINGS=true`）
- プレイヤーはゲームクライアントの **Join Game** から `192.168.10.31:7777` で直接接続する
- 詳細な運用手順は [Runbook](../../docs/project_docs/T-20260228-005-starrupture-manage-server-runbook/plan.md) を参照

## 構成

| リソース | ファイル | 説明 |
|----------|----------|------|
| Namespace | `namespace.yaml` | `starrupture` namespace |
| Deployment | `deployment.yaml` | ゲームサーバー本体（replicas: 1） |
| Service | `service.yaml` | LoadBalancer（`192.168.10.31:7777`、UDP/TCP） |
| PVC | `pvc.yaml` | セーブデータ永続化（Longhorn, 5Gi） |
| Application | `application.yaml` | ArgoCD アプリケーション定義 |
| Kustomization | `kustomization.yaml` | Kustomize リソース定義 |

## 接続情報

- **IP**: `192.168.10.31`
- **Port**: `7777`（UDP/TCP）
- **接続方法**: ゲームクライアント → Join Game → IP直接入力

## 関連ドキュメント

- [運用 Runbook（T-20260228-005）](../../docs/project_docs/T-20260228-005-starrupture-manage-server-runbook/plan.md)
- [PVC 障害調査（T-20260225）](../../docs/project_docs/T-20260225-starrupture-pvc-investigation/plan.md)
