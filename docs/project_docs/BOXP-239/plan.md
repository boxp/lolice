# BOXP-239: 停止中sdnextの参照更新

arch PR #13678でnoble修復済み。新latest digestはc310d789a913ef1729768c8c8ce4cba5bcc5d04c8f7401c9f2861a4e5f54fe94。

公開後もstable-diffusionのApplication.status.summary.imagesが空でImageUpdater.status.imagesManaged=0のため、既存の自動write-backが動かない。公式仕様と既存arc-runners設定に従い、sdnext-ipexのcommonUpdateSettings.forceUpdateだけをtrueにする。これは稼働imageに含まれなくても更新対象にする設定で、Deploymentのscaleを変更しない。

1. 実cluster CRDでschema検証、kustomize build、codex-review、通常PR/CIを実施。
2. merge後は既存ArgoCD自動syncとImageUpdaterのwrite-backを待ち、新digestとの一致を確認。
3. stable-diffusion/cloudflared replicas 0とlocal-llm無変更を再確認。SD.Next起動・GPU生成は行わない。

正本: Obsidian Projects/BOXP-236/sdnext-ipex-repair.md。
rollback: 本PRを通常PRでrevert（停止中の自動追従が無効に戻る）。image自体のrollbackは正本に記載のtag固定と参照変更を同時に行う。
