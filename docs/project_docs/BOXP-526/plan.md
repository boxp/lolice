# BOXP-526: codex-workspace home の容量対策

## 目的
対象 volume の backup 用 snapshot の上乗せを恒久的に減らし、安全に home PVC を拡張する。

## 段階
1. この変更で対象 volume の Completed backup に対応する snapshot を削除し、上乗せを恒久削減する。S3 backup は維持する。
2. 削減後に actualSize と各 node の物理空きを再計測し、移動後も golyat-1 の物理空きが最低 25% 保たれる場合だけ golyat-2 から golyat-1 に副本を移動する。別 PR とし、権限を限定する。
3. 別 PR で `argoproj/codex-workspace/pvc.yaml` を 150Gi に変更し、オンライン拡張する。
4. df、2 副本の健全性、業務復帰を確認する。

## 安全条件と停止条件
volume が healthy で、対応する S3 backup が Completed の snapshot のみ削除する。S3 backup は削除しない。削減後の actualSize と node の物理空きを実測し、移動後の空き 25% を確認できない場合、volume または副本が健全でない場合、backup の完了を確認できない場合、拡張や業務復帰の確認に異常がある場合は次の段階を停止する。200GiB への拡張、購入、副本削減、広域権限は行わない。
