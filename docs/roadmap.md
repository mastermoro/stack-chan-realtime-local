# Initial GitHub roadmap

These items should become GitHub Issues after the remote repository is created.

## P0 - Bootstrap / validation

1. Deploy first Container Apps revision and measure first-audio latency.
2. Validate Relay -> `gpt-realtime-2.1` session with Managed Identity in the deployed environment.
3. Validate Realtime `search_web` function call -> `gpt-5.6-terra` Responses API `web_search` -> citation return in the deployed environment.

## P1 - Production hardening

1. Replace static device token map with managed device credential lifecycle.
2. Add Application Insights custom metrics and correlation IDs.
3. Add rate limits and maximum concurrent session controls.
4. Add Web Search query privacy filtering and domain policy profiles.
5. Add long-running session soak tests and metrics for Relay/Realtime reconnects.

## P2 - Conversation quality

1. Tune Server VAD threshold and silence duration on actual Stack-chan hardware.
2. Tune the implemented expression mapping and search animation from on-device feedback.
3. Add barge-in with robust response cancellation and playback truncation.
4. Evaluate AEC and full-duplex mode.
