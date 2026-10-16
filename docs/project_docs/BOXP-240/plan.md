# BOXP-240 SD.Next GPU実機検証

## 目的と範囲

修復済みsdnext-ipexの固定digestをGPU実機で起動し、XPU認識・固定seedで画像1枚・installer後の--test139を検証する。結果はObsidianのProjects/BOXP-236/sdnext-ipex-gpu-verification.mdへ記録する。

## 手順

1. image/replicas/Argo sync/GPU所有/LLM処理中・待機要求/model cacheを保存する。
2. 既存llm-sd-deploy workflowでLLM=0、SD=1、cloudflared=0のPRを生成し、差分reviewとCI後にmergeする。GPU allocatable=1による排他とLLM停止を確認しSD試験へ進む。
3. 既存safetensorsモデルを使用し、追加download・remote codeなし、LAN/cluster限定経路で無害なprompt、seed固定、batch1の小さい画像を生成する。画像実体・寸法・XPUログ・例外を確認する。
4. GPUなし--testと実機を比較し、139の原因/仮説/未確認を区別する。
5. 成否を問わず既存workflowのllm選択でLLM=1、SD=0、cloudflared=0へ戻し、Ready/healthと既存モデルの短い推論を確認する。元のloadedモデル状態も復元する。

## 制約と復旧

外部公開、認証・NetworkPolicy変更、他run停止、lock削除、モデル変更/削除、OS/driver更新は行わない。LLMが処理中ならmergeせず待機する。Argo selfHeal有効のためkubectl scale単独は使わない。起動不能・可用性劣化時は再試行より復旧を優先する。
