**日本語** | [English](../en/adr/0003-relay-as-agent-boundary.md)

# ADR-0003: Relay を Agent Orchestrator の境界とする

ステータス: 承認済み

## 決定

Relay は透過的な音声プロキシではない。認証、セッションライフサイクル、ツールの登録と実行、Web Search、ポリシー適用、可観測性を担う。

## 結果

MCP、外部 API、社内ナレッジ検索などの将来のツールは、ファームウェアプロトコルを変更せずに Relay の背後へ追加できる。
