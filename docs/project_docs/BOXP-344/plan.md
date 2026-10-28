# BOXP-344 閉じた合成評価の入口制御

## 範囲

合成設定1件・合成小説1件、各1枚まで。archの隔離ハーネスと組み合わせる準備であり、通常Boardのキューや既存作品は接続しない。モデル・既定replicas・権限は変更しない。実画像が揃うまで評価完了としない。

## 実装

- 共通のEnvoyに projected ConfigMap `llama-server-admission` の `state` を読むLua filterを追加。`closed:<run-token>` はhealth以外の新規要求を503にする。受付済みstreamは切らない。不正・欠損ファイルも閉じる。stateだけのGitOps変更ではPodを再起動しない。
- llama-serverの8081をloopback限定にし、Service経由とPod IP直通のどちらもEnvoyの受付判定を通す。startup/readinessはEnvoyのhealth経由、livenessはcontainer内curlでloopbackへ直接問い合わせる。Envoy障害だけで正常なllama-serverを再起動しない。
- Envoy adminはloopback:9903へ移し、従来のmetrics:9901はGET限定のstats/server_infoとmodel別slots/modelsの観測窓口にする。LAN Serviceにmetricsは公開しない。
- health/slots/modelsは別upstream clusterとし、推論のactive/pending統計を観測要求自身で増やさない。

## 検証と配備条件

`python3 tests/local-llm/test_admission.py` は本番と同じEnvoy digestと合成HTTP backendをnetwork:noneで起動し、新規拒否、長時間要求の完走、active/pending=0、不正state、再開、admin操作拒否、観測pathのrewriteを検証する。GPU・本番APIは使用しない。

通常のPR・独立レビュー・CIを経てから配備し、すべての実Podで設定とstateをreadbackする。ConfigMap反映には遅延があり、merge/Syncedだけでは封鎖完了としない。Pod再作成・UID変化があれば観測をやり直す。全Service/LANとPod経路の拒否、active/pending=0の安定、loaded model全件のslots idleを確認する。切断されたクライアントの推論はupstream統計だけでは判断できないためslots確認を省略しない。slotsを読み出せない／未ロードモデルへの照会がロードを起こす場合は切替しない。

追加の必要条件は全自動投入の停止、他の直接GPU利用者とBOXP-337の不在、既存のGitOps desired state/Pod/GPU解放の照合、Qwenジョブ投入経路、Qwen停止とGemma復帰後のhealth・短い推論の確認。これらはEnvoy gate単体では証明できず、未接続の実アダプターを実装したと偽らない。新credential/RBAC/強制kill/lock解除は行わない。

## 復帰とrollback

通常復帰はQwen停止・GPU解放確認→Gemma desired stateと自動投入復元→health・短い推論→`open:<run-token>`をGitOpsで反映→全Pod・両Serviceで再開readback。確認不能ならgateは閉じたままにする。

この準備PR自体のrollbackは通常Git revert/PR/Argo反映。評価中にgate実装をrevertすると封鎖が失われるため、Qwen停止とGemma復帰を確認するまでrevertしない。ConfigMap stateの開放だけを復帰の代わりにしない。

## 復帰観測の制限

`verify:<token>` は一般推論を閉じたまま、固定の合成短文・既定Gemma・4tokenの復帰probeだけを許可する。依頼者のbody/modelは転送しない。`closed`ではprobeも拒否する。verifyへの変更はQwen=0を実確認してからGemma=1と同じGitOps段階で行う。

routerの`GET /slots?model=...`は未ロードmodelをロードし得るため、観測窓口はclosed/verify時のみ、`/models`で既にloadedと返るidに限定する。未知schemaや未ロードmodelは503。Qwen workloadはegressも全拒否し、既存wheelの存在確認後のみ起動する。

## 進捗

実機切替・実画像生成・文章復帰は未実施。初期stateは`open:baseline`、Gemma=1/Qwen=0のまま。live readbackと2枚のPNGを別途記録する。
