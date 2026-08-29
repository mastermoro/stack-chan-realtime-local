[日本語](../../adr/0001-web-only-rag.md) | **English**

# ADR-0001: Use Web Search as the only initial RAG knowledge source

Status: Accepted

## Decision

The initial release uses Microsoft Foundry Responses API `web_search` as its only external knowledge source. Azure AI Search, vector databases, embeddings, document ingestion, and internal knowledge indexes are out of scope.

## Consequences

- Initial infrastructure and operations are substantially simpler.
- Current/public information can be retrieved with citations.
- Queries sent to web search must not contain confidential/internal data without an explicit later policy change.
- The Relay keeps a generic tool boundary so internal RAG or MCP can be added later without changing the Stack-chan protocol.
