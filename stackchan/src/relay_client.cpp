#include "relay_client.hpp"

#include <ArduinoJson.h>

#include "secrets.hpp"

namespace stackchan {
namespace {
// The Relay/ESP32 TCP path can be silently reclaimed after roughly 30 seconds
// without traffic. Keep the application session active independently of audio.
constexpr uint32_t kKeepaliveIntervalMs = 10'000;
}

void RelayClient::begin_wifi(const char* host, uint16_t port) {
  end();
  transport_ = RelayTransport::Wifi;
  String headers = String("Authorization: Bearer ") + DEVICE_TOKEN + "\r\n" +
                   "X-Device-Id: " + DEVICE_ID + "\r\n";
  ws_.setExtraHeaders(headers.c_str());
#if RELAY_USE_TLS
  ws_.beginSSL(host, port, RELAY_PATH);
#else
  ws_.begin(host, port, RELAY_PATH);
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

void RelayClient::begin_usb() {
  end();
  transport_ = RelayTransport::Usb;
  last_usb_open_ms_ = 0;
  send_usb_open();
  send_notice("USB CONNECTING", "Opening Relay through PC bridge");
}

void RelayClient::send_usb_open() {
  if (transport_ != RelayTransport::Usb || !usb_.host_available()) return;
  JsonDocument doc;
  doc["device_id"] = DEVICE_ID;
  doc["device_token"] = DEVICE_TOKEN;
  String json;
  serializeJson(doc, json);
  usb_.send(UsbFrameType::DeviceOpen, reinterpret_cast<const uint8_t*>(json.c_str()),
            json.length());
  last_usb_open_ms_ = millis();
}

void RelayClient::end() {
  const RelayTransport previous = transport_;
  transport_ = RelayTransport::None;
  connected_ = false;
  if (previous == RelayTransport::Wifi) {
    ws_.disconnect();
  } else if (previous == RelayTransport::Usb) {
    usb_.send(UsbFrameType::DeviceClose);
  }
}

void RelayClient::loop() {
  static bool usb_initialized = false;
  if (!usb_initialized) {
    usb_.begin();
    usb_.on_frame([this](UsbFrameType type, const uint8_t* payload, size_t length) {
      handle_usb_frame(type, payload, length);
    });
    usb_initialized = true;
  }
  usb_.loop();
  if (transport_ == RelayTransport::Wifi) ws_.loop();
  if (transport_ == RelayTransport::Usb && !connected_ && usb_.host_available() &&
      (last_usb_open_ms_ == 0 || millis() - last_usb_open_ms_ >= 2000)) {
    send_usb_open();
  }
  if (!connected_) return;

  const uint32_t now = millis();
  if (static_cast<uint32_t>(now - last_keepalive_ms_) < kKeepaliveIntervalMs) return;
  last_keepalive_ms_ = now;
  send_control("ping");
  Serial.println("Relay keepalive sent");
}

void RelayClient::send_audio(const int16_t* samples, size_t sample_count) {
  if (!connected_ || sample_count == 0) return;
  const auto* data = reinterpret_cast<const uint8_t*>(samples);
  const size_t length = sample_count * sizeof(int16_t);
  if (transport_ == RelayTransport::Wifi) {
    ws_.sendBIN(data, length);
  } else if (transport_ == RelayTransport::Usb) {
    usb_.send(UsbFrameType::DeviceBinary, data, length);
  }
}

void RelayClient::send_control(const char* type) {
  if (!connected_) return;
  JsonDocument doc;
  doc["type"] = type;
  String json;
  serializeJson(doc, json);
  send_text(json);
}

void RelayClient::send_ui_mode(const char* mode) {
  if (!connected_) return;
  JsonDocument doc;
  doc["type"] = "ui.mode";
  doc["mode"] = mode;
  String json;
  serializeJson(doc, json);
  send_text(json);
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
  send_text(json);
}

bool RelayClient::send_text(const String& text) {
  if (transport_ == RelayTransport::Wifi) {
    String mutable_text(text);
    return ws_.sendTXT(mutable_text);
  }
  if (transport_ == RelayTransport::Usb) {
    return usb_.send(UsbFrameType::DeviceText,
                     reinterpret_cast<const uint8_t*>(text.c_str()), text.length());
  }
  return false;
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
  if (transport_ != RelayTransport::Wifi) return;
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
      handle_text(payload, length);
      break;
    }
    default:
      break;
  }
}

void RelayClient::handle_usb_frame(UsbFrameType type, const uint8_t* payload, size_t length) {
  if (type == UsbFrameType::HostProbe) {
    JsonDocument doc;
    doc["protocol"] = kProtocolVersion;
    doc["device_id"] = DEVICE_ID;
    String json;
    serializeJson(doc, json);
    usb_.send(UsbFrameType::DeviceStatus, reinterpret_cast<const uint8_t*>(json.c_str()),
              json.length());
    return;
  }
  if (transport_ != RelayTransport::Usb) return;
  switch (type) {
    case UsbFrameType::HostOpenAck:
      connected_ = true;
      last_keepalive_ms_ = millis();
      send_notice("USB CONNECTED", "Authenticating with Relay");
      send_hello();
      break;
    case UsbFrameType::HostClose:
      connected_ = false;
      send_notice("USB DISCONNECTED", "PC bridge connection lost");
      if (state_handler_) state_handler_(AgentState::Disconnected);
      break;
    case UsbFrameType::HostText:
      handle_text(payload, length);
      break;
    case UsbFrameType::HostBinary:
      if (audio_handler_) audio_handler_(payload, length);
      break;
    default:
      break;
  }
}

void RelayClient::handle_text(const uint8_t* payload, size_t length) {
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
    Serial.println("Relay response.done received; keeping transport session open");
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
}
}  // namespace stackchan
