# boxp-home

boxp-homeプロジェクトは、自宅のサービスへのアクセスを提供するCloudflare Tunnelを管理するためのプロジェクトです。

## 概要

- **目的**: 自宅のサービスにセキュアにアクセスするためのCloudflare Tunnelの提供
- **管理**: ArgoCD による GitOps 管理
- **シークレット管理**: AWS Systems Manager Parameter Store からの自動同期

## 構成要素

- `namespace.yaml`: boxp-home 名前空間の定義
- `external-secret.yaml`: AWS SSM Parameter Store からのトンネルトークン同期
- `cloudflared-deployment.yaml`: Cloudflared の Deployment 定義
- `service-cloudflared-metrics.yaml`: メトリクス監視用 Service
- `application.yaml`: ArgoCD Application 定義

## デプロイ

このプロジェクトは ArgoCD によって自動的に管理されます。変更をコミットすると、ArgoCD が自動的に同期を行います。

## 前提条件

- AWS SSM Parameter Store に `boxp-home-tunnel-token` パラメータが設定されていること
- External Secrets Operator が動作していること 