# BOXP-349 限定runtime transportと所有分担

2026-10-28 Ownerは専用SAへの2 Deployment scale / 1受付ConfigMapのget,patchとArgoの名指し3field移管を承認済み。

既存Envoy gate/drain/slots/GPU解放/固定復帰probeを使い、runtime phase制御はarchに実装する。本repoは専用SA/Role/Binding、projected tokenを保持する専用transport Pod、名指しArgo所有分担を配備する。通常runnerへの権限追加やcredential配送は行わない。

1. 専用node file queue transportを実装し、任意resource・無条件patch・replicas範囲外を拒否する。実行前intent永続化、結果不明は自動再送しない。
2. 独立レビュー・合成負例試験・kustomize/CIを通す。
3. nodeの空の専用queue dirだけを既存SSH経路で作り、通常PR merge/GitOps反映を確認する。
4. 親子Appが独立同期するため、まず既存gate値をGitで維持したまま所有分担を同期・readbackする。その確認後に別の通常PRでGit初期値をclosed:runtime-bootstrapへ変更する。既存resourceはRespectIgnoreDifferencesにより現在値を保ち、再作成時はGitのclosed:runtime-bootstrap/Qwen0で閉じる。他fieldのselfHeal/pruneは維持。旧GitOps GPU切替workflowはworkflow scope不足のためGitファイル変更を除外し、既存GitHub APIでdisabled_manuallyを確認した。再有効化はruntime所有契約を壊すため行わない。
5. arch controllerから切替・実画像1枚・復帰を実測。原典と画像はlocal限定、cloud担当は非本文metadata/合成試験のみ。

SAはlocal-llm、named Scale2個とnamed ConfigMap1個のみget/patch。Deployment本体、pods/exec、Secret、RBAC編集、cluster-adminは付与しない。fieldのRBAC制限は不可能だがtransportは条件付き /data/state 以外を拒否する。workerは既存workspace image digestを固定し、workspace/PVC/model/外部API credentialはmountしない。node上のruntime-apiディレクトリへアクセスできるuid1000を信頼境界とする。

rollbackは先にQwen停止/Pod消失/GPU解放→Gemma health/短文推論→openを確認し、Gitの3値を実状態と合わせる通常PRを作ってから対象ignoreを解除する。生成中にignoreだけ削除したりopenへ単純revertしない。model/PVC/既存画像/lockは削除しない。

## 配備readback

2026-10-28: #930 CI/独立レビュー後merge。専用transport 1/1と実Scale GETを確認。arch ownership_check.pyで両Appの名指し3field/RespectIgnoreDifferences/selfHeal/prune/Syncedを確認後に、本bootstrap初期値変更を作成した。既存live gateはopenを保ち、再作成時だけclosedで起動する。
