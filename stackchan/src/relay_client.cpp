#include "relay_client.hpp"

#include <ArduinoJson.h>

#include "secrets.hpp"

namespace stackchan {
namespace {
// The Relay/ESP32 TCP path can be silently reclaimed after roughly 30 seconds
// without traffic. Keep the application session active independently of audio.
constexpr uint32_t kKeepaliveIntervalMs = 10'000;
}

void RelayClient::begin() {
  String headers = String("Authorization: Bearer ") + DEVICE_TOKEN + "\r\n" +
                   "X-Device-Id: " + DEVICE_ID + "\r\n";
  ws_.setExtraHeaders(headers.c_str());
#if RELAY_USE_TLS
  ws_.beginSSL(RELAY_HOST, RELAY_PORT, RELAY_PATH);
#else
  ws_.begin(RELAY_HOST, RELAY_PORT, RELAY_PATH);
#endif
  ws_.setReconnectInterval(5000);
  // Send WebSocket protocol pings as well as the application keepalive below.
  // A zero disconnect count makes this diagnostic/liveness traffic non-fatal:
  // TCP reconnect remains the recovery path if the speaker/audio loop is busy.
  ws_.enableHeartbeat(10'000, 30'000, 0);
  ws_.onEvent([this](WStype_t type, uint8_t* payload, size_t length) {
    handle_event(type, payload, length);
  });
}

void RelayClient::loop() {
  ws_.loop();
  if (!connected_) return;

  const uint32_t now = millis();
  if (static_cast<uint32_t>(now - last_keepalive_ms_) < kKeepaliveIntervalMs) return;
  last_keepalive_ms_ = now;
  send_control("ping");
  Serial.println("Relay keepalive sent");
}

void RelayClient::send_audio(const int16_t* samples, size_t sample_count) {
  if (!connected_ || sample_count == 0) return;
  ws_.sendBIN(reinterpret_cast<const uint8_t*>(samples), sample_count * sizeof(int16_t));
}

void RelayClient::send_control(const char* type) {
  if (!connected_) return;
  JsonDocument doc;
  doc["type"] = type;
  String json;
  serializeJson(doc, json);
  ws_.sendTXT(json);
}

void RelayClient::send_ui_mode(const char* mode) {
  if (!connected_) return;
  JsonDocument doc;
  doc["type"] = "ui.mode";
  doc["mode"] = mode;
  String json;
  serializeJson(doc, json);
  ws_.sendTXT(json);
}

void RelayClient::send_notice(const char* title, const char* detail) {
  if (notice_handler_) notice_handler_(title, detail);
}

void RelayClient::send_hello() {
  JsonDocument doc;
  doc["type"] = "hello";
  doc["protocol"] = kProtocolVersion;
  doc["device_id"] = DEVICE_ID;
  doc["audio"]["format"] = "pcm16";
  doc["audio"]["sample_rate"] = kSampleRate;
  doc["audio"]["channels"] = kChannels;
  String json;
  serializeJson(doc, json);
  ws_.sendTXT(json);
}

AgentState RelayClient::parse_state(const char* value) {
  if (!value) return AgentState::Error;
  if (!strcmp(value, "ready")) return AgentState::Ready;
  if (!strcmp(value, "listening")) return AgentState::Listening;
  if (!strcmp(value, "thinking")) return AgentState::Thinking;
  if (!strcmp(value, "searching")) return AgentState::Searching;
  if (!strcmp(value, "speaking")) return AgentState::Speaking;
  return AgentState::Error;
}

void RelayClient::handle_event(WStype_t type, uint8_t* payload, size_t length) {
  switch (type) {
    case WStype_CONNECTED:
      connected_ = true;
      last_keepalive_ms_ = millis();
      Serial.println("Relay WebSocket connected");
      send_notice("CONNECTED", "Authenticating with Relay");
      send_hello();
      break;
    case WStype_DISCONNECTED:
      connected_ = false;
      Serial.printf("Relay WebSocket disconnected (reason bytes=%u)\n",
                    static_cast<unsigned>(length));
      if (length > 0) {
        Serial.printf("Relay disconnected: %.*s\\n", static_cast<int>(length), payload);
      }
      send_notice("DISCONNECTED", "Relay connection lost; retrying");
      if (state_handler_) state_handler_(AgentState::Disconnected);
      break;
    case WStype_ERROR:
      Serial.printf("Relay WebSocket error (detail bytes=%u)\n", static_cast<unsigned>(length));
      if (length > 0) {
        Serial.printf("Relay WebSocket error: %.*s\\n", static_cast<int>(length), payload);
      }
      send_notice("CONNECTION ERROR", "WebSocket connection failed");
      if (state_handler_) state_handler_(AgentState::Error);
      break;
    case WStype_BIN:
      if (audio_handler_) audio_handler_(payload, length);
      break;
    case WStype_TEXT: {
      // Search citations can exceed the old fixed 512-byte document. ArduinoJson
      // 7 grows this document as needed, avoiding a silent parse failure.
      JsonDocument doc;
      if (deserializeJson(doc, payload, length)) return;
      const char* message_type = doc["type"] | "";
      if (!strcmp(message_type, "state") && state_handler_) {
        state_handler_(parse_state(doc["state"] | ""));
      } else if (!strcmp(message_type, "session.ready")) {
        send_notice("SESSION READY", "Relay connected to Foundry");
      } else if (!strcmp(message_type, "session.reconnected")) {
        send_notice("SESSION RECONNECTED", "Foundry reconnected; ready to listen");
        if (session_reconnected_handler_) session_reconnected_handler_();
      } else if (!strcmp(message_type, "hello.ack")) {
        send_notice("DEVICE READY", "Tap the screen to speak");
      } else if (!strcmp(message_type, "response.done")) {
        Serial.println("Relay response.done received; keeping WebSocket session open");
        send_notice("RESPONSE DONE", "Waiting for next turn");
        if (response_done_handler_) response_done_handler_();
      } else if (!strcmp(message_type, "sources")) {
        const char* title = doc["sources"][0]["title"] | "Web search completed";
        send_notice("WEB SEARCH", title);
      } else if (!strcmp(message_type, "emotion")) {
        const char* emotion = doc["emotion"] | "neutral";
        if (emotion_handler_) emotion_handler_(emotion);
      } else if (!strcmp(message_type, "notice")) {
        send_notice(doc["title"] | "NOTICE", doc["detail"] | "");
      } else if (!strcmp(message_type, "error")) {
        const char* code = doc["code"] | "RELAY ERROR";
        const char* error_message = doc["message"] | "Unknown Relay error";
        if (strcmp(code, "UPSTREAM_RECONNECTING")) {
          if (state_handler_) state_handler_(AgentState::Error);
        }
        send_notice(code, error_message);
      }
      break;
    }
    default:
      break;
  }
}
}  // namespace stackchan
