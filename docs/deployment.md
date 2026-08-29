**日本語** | [English](en/deployment.md)

# Azure Container Apps への任意デプロイ

このドキュメントは上流リポジトリから継承したものである。標準の `stack-chan-realtime-local` デプロイでは、Stack-chan と同じ LAN 内の Windows PC で Relay を実行する。以下の手順は、任意の代替デプロイ方法としてのみ残している。

## 必要な既存 AI リソース

初期インフラストラクチャでは、次のデプロイを含む既存の Microsoft Foundry / Azure OpenAI リソースを前提とする。

- Realtime デプロイ。既定のデプロイ名は `gpt-realtime-2.1`
- `web_search` に使用する Responses 対応モデルのデプロイ。現在は `gpt-5.6-terra`

Relay はリソースエンドポイント `https://<resource>.openai.azure.com` を使用する。

## Container Apps

`infra/main.bicep` は次のリソースをプロビジョニングする。

- Log Analytics ワークスペース
- Azure Container Apps 環境
- ポート 8080 の外部 HTTPS Container App イングレス
- システム割り当てマネージド ID
- 最小 1 レプリカ

コンテナーイメージはパラメーターとして指定するため、イメージのビルドと発行は GitHub Actions または既存の ACR プロセスに残すことができる。

## RBAC

Relay のシステム割り当て ID に対して、AI リソースのスコープで適切な Foundry 推論ロールを割り当てる。現在の Foundry User ロール定義 ID は、任意のロール割り当てモジュールとして `infra/rbac.bicep` に記載されている。

## デバイストークン

v0.1 では、`DEVICE_TOKENS_JSON` を環境シークレットとする。複数の本番デバイスを導入する前に、デバイス認証情報のライフサイクルを専用のシークレットまたは ID の仕組みに移行する。

実際の値を `infra/main.bicepparam` に記載してはならない。このファイルはコミット済みのテンプレートである。デバイストークンの JSON は、安全なデプロイパラメーターまたは CI のシークレットストアを介して渡す。Container Apps が公開するのは HTTPS/WSS イングレスのみだが、`/healthz` と `/readyz` はプラットフォームのプローブ用に意図的に未認証としている。Realtime WebSocket エンドポイント自体にはデバイストークンが必要である。
