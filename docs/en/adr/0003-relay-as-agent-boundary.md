[日本語](../../adr/0003-relay-as-agent-boundary.md) | **English**

# ADR-0003: Treat Relay as the Agent Orchestrator boundary

Status: Accepted

## Decision

The Relay is not a transparent audio proxy. It owns authentication, session lifecycle, tool registration/execution, Web Search, policy enforcement, and observability.

## Consequences

Future tools such as MCP, external APIs, or internal knowledge search can be added behind the Relay without firmware protocol changes.
