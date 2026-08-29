[日本語](../deployment.md) | **English**

# Optional Azure Container Apps deployment

This document is inherited from the upstream repository. The standard
`stack-chan-realtime-local` deployment runs the Relay on a Windows PC in the
same LAN as Stack-chan. The instructions below remain available only as an
optional deployment alternative.

## Required existing AI resources

Initial infrastructure expects an existing Microsoft Foundry / Azure OpenAI resource containing:

- a Realtime deployment, default deployment name `gpt-realtime-2.1`
- a Responses-capable model deployment used for `web_search`, currently
  `gpt-5.6-terra`

The relay uses the resource endpoint `https://<resource>.openai.azure.com`.

## Container Apps

`infra/main.bicep` provisions:

- Log Analytics workspace
- Azure Container Apps environment
- external HTTPS Container App ingress on port 8080
- system-assigned managed identity
- minimum one replica

The container image is supplied as a parameter so image build/publish can remain in GitHub Actions or an existing ACR process.

## RBAC

Assign the relay's system-assigned identity the appropriate Foundry inference role at the AI resource scope. The current Foundry User role definition ID is documented in `infra/rbac.bicep` as an optional role assignment module.

## Device tokens

For v0.1, `DEVICE_TOKENS_JSON` is an environment secret. Before multiple production devices, move device credential lifecycle to a dedicated secret/identity mechanism.

Do not place real values in `infra/main.bicepparam`; it is a committed template.
Pass the device-token JSON through a secure deployment parameter or your CI
secret store. Container Apps exposes only HTTPS/WSS ingress, but `/healthz` and
`/readyz` are intentionally unauthenticated for platform probes; the realtime
WebSocket endpoint itself requires a device token.
