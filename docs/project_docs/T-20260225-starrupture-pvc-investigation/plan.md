# T-20260225: StarRupture PVC Mount Failure Investigation

## 1. 事象サマリ

StarRupture dedicated server の Deployment を replicas 0→1 に復帰（2026-02-25）した際、Pod が `ContainerCreating` 状態で停止し、コンテナが起動しない。

**観測された症状:**
- Pod `starrupture-79c676bf67-jmfn4` が Pending (ContainerCreating) で5時間以上停止
- 5時間の間に3つの異なるReplicaSet (6b6767749b, 654f8b88c4, 79c676bf67) が作成された
- PVC `starrupture-saved-claim` は Bound 状態（正常）
- Longhorn ボリューム `pvc-0aea6d3c-017d-40d1-8f6e-7e43e9439a37` の robustness は healthy
- しかしボリュームの state は **detached (2)** のまま

## 2. 根本原因

### 主因: Longhorn ボリューム attach/detach 状態不整合

K8s の VolumeAttachment オブジェクト（CSI ControllerPublishVolume）は作成成功したが、
Longhorn 内部ではボリュームエンジンが起動せず `detached` 状態が継続。

**エビデンス:**
- kube-controller-manager: `AttachVolume.Attach succeeded` (09:40:05)
- Longhorn volume state: `detached (2)` が5時間以上継続（Prometheus range query で確認）
- golyat-2 からは `attaching (3)` が50分間続いた後にメトリクス報告が停止

**推定メカニズム:**
1. 14日間の休止（replicas=0, Feb 11〜Feb 25）中にボリュームが完全にデタッチ
2. replicas=1 復帰時にCSI attach phase は完了（VolumeAttachment作成）
3. しかしLonghornエンジンプロセスが起動せず、ボリュームが実際にはアタッチされない
4. kubeletの NodeStageVolume/NodePublishVolume がマウントできずContainerCreating

### 副因: ArgoCD SharedResourceWarning による reconciliation ループ

`starrupture/kustomization.yaml` に `application.yaml` が含まれており、
`argocd-apps` Application と `starrupture` Application の両方が同じ Application リソースを管理。

**エビデンス:**
- ArgoCD log: `Application/starrupture is part of applications argocd/argocd-apps and starrupture`
- 5時間の間に3つの異なるReplicaSetが生成（Deployment pod template の変更を示唆）
- selfHeal による頻繁なreconciliation がボリュームattachプロセスを中断

## 3. 修正案

### 3.1 マニフェスト修正（本PR）

#### kustomization.yaml から application.yaml を除去
- **理由:** SharedResourceWarning を解消し、reconciliation ループを停止
- **影響:** Application リソースは親の `argocd-apps` が引き続き管理するため無影響
- **ファイル:** `argoproj/starrupture/kustomization.yaml`

#### replicas を一時的に 0 に設定
- **理由:** 現在の stuck pod と stale VolumeAttachment をクリーンアップ
- **手順:** この PR マージ後、ArgoCD sync → VolumeAttachment 削除 → 後続 PR で replicas=1 に復帰
- **ファイル:** `argoproj/starrupture/deployment.yaml`

### 3.2 運用手順（PR マージ後に実施）

1. **PR マージ**: ArgoCD auto-sync により replicas=0 が適用される
2. **VolumeAttachment 確認・削除**:
   ```bash
   kubectl get volumeattachments | grep pvc-0aea6d3c
   kubectl delete volumeattachment <name>
   ```
3. **Longhorn UI で確認**: ボリュームが `detached` → 正常状態に戻ることを確認
4. **replicas 復帰**: 別 PR で `replicas: 1` に戻す
5. **動作確認**: Pod が Running になり、ゲームサーバーが起動することを確認

## 4. リスクと確認項目

| リスク | 影響 | 対策 |
|--------|------|------|
| replicas=0 によるサービス停止 | StarRupture サーバーが一時的に利用不可 | 既に ContainerCreating で利用不可のため影響なし |
| application.yaml 除去による ArgoCD 管理漏れ | Application リソースが管理されなくなる | 親の argocd-apps が管理しているため問題なし |
| VolumeAttachment 削除失敗 | ボリュームが再アタッチできない | Longhorn UI から force detach 可能 |
| データ消失 | セーブデータの喪失 | PVC は Bound (healthy)、Longhorn backup あり (S3) |

## 5. 再発防止

- ArgoCD Application の自己参照パターンを段階的に解消（palserver, ark-survival-ascended も同様）
- Longhorn ボリュームの attach 状態監視アラートを追加検討
- 長期休止後の復帰手順をドキュメント化
