# Kubernetes Manifest Validation GitHub Action 準備ドキュメント

## 目的
本アクションは、リポジトリ内の Kubernetes マニフェストファイル（*.yaml, *.yml）の構文およびスキーマ検証を行い、不正な構成がクラスタへデプロイされる前に事前検出することを目的とする。

## 背景
- Kubernetes マニフェストは、クラスタの望ましい状態を定義する YAML ファイルである。
- 検証方法としては、kubectl の --dry-run オプションや、[kubeval](https://github.com/instrumenta/kubeval) ツールが利用される。
- kubeval は、Kubernetes API スキーマに基づいた検証を実施し、誤った設定値やフォーマットエラーを早期に検出する。
- GitHub Actions を用いてプルリクエスト時に自動検証を実行することで、マージ前に問題のある変更を防止できる。

## 調査・参考情報
- [kubeval GitHub リポジトリ](https://github.com/instrumenta/kubeval)
- [GitHub Actions 公式ドキュメント](https://docs.github.com/ja/actions)
- 他のプロジェクトでの同様の GitHub Action 実装例

## 要件
- プルリクエスト作成時に自動で実行される
- デフォルトで `argoproj` ディレクトリ内の *.yaml, *.yml ファイルを対象とする
- kubeval の --strict オプションにより厳密な検証を実施
- manifests ディレクトリが存在しない場合、警告メッセージを出して検証をスキップ

