#pragma once

// CI/default build values. deploy-from-relay.ps1 generates relay_endpoint.hpp
// with the Windows PC address selected for the current LAN.
#define RELAY_HOST "192.168.1.10"
#define RELAY_PORT 8080
#define RELAY_PATH "/v1/realtime"
#define RELAY_USE_TLS 0
