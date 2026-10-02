#include "relay_client.hpp"

#include <ArduinoJson.h>

#include "secrets.hpp"

namespace stackchan {
namespace {
// The Relay/ESP32 TCP path can be silently reclaimed after roughly 30 seconds
// without traffic. Keep the application session active independently of audio.
constexpr uint32_t kKeepaliveIntervalMs = 10'000;
// An ordered acknowledgement normally arrives within one round trip. Allow
// five seconds for busy links, then recover a lost/corrupt ack by replacing
// the transport session. A deadline must never unmute the existing stream.
constexpr uint32_t kInterruptAckTimeoutMs = 5000;
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
  if (interrupt_recovery_id_ != 0) doc["recovery_id"] = interrupt_recovery_id_;
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
  interrupt_recovery_id_ = 0;
  context_reset_notice_pending_ = false;
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
  if (pending_interrupt_id_ != 0 &&
      static_cast<uint32_t>(now - interrupt_started_ms_) >= kInterruptAckTimeoutMs) {
    recover_interrupt_timeout();
    return;
  }
  if (static_cast<uint32_t>(now - last_keepalive_ms_) < kKeepaliveIntervalMs) return;
  last_keepalive_ms_ = now;
  send_control("ping");
  Serial.println("Relay keepalive sent");
}

void RelayClient::recover_interrupt_timeout() {
  // Keep pending_interrupt_id_ closed until a real fresh handshake. Marking
  // disconnected also stops upstream capture and makes late pause acks inert.
  interrupt_recovery_id_ = pending_interrupt_id_;
  connected_ = false;
  Serial.printf("Relay interrupt ack timed out id=%lu; reconnecting transport\n",
                static_cast<unsigned long>(interrupt_recovery_id_));
  if (transport_ == RelayTransport::Wifi) {
    // WebSocketsClient retains its endpoint and applies its normal reconnect
    // interval after disconnect(). No additional reconnect timer is needed.
    ws_.disconnect();
  } else if (transport_ == RelayTransport::Usb) {
    usb_.send(UsbFrameType::DeviceClose);
    last_usb_open_ms_ = 0;  // Existing DeviceOpen retry path performs recovery.
  }
  if (state_handler_) state_handler_(AgentState::Disconnected);
  send_notice("INTERRUPT TIMEOUT", "Reconnecting Relay; conversation will reset");
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

bool RelayClient::send_control(const char* type) {
  if (!connected_) return false;
  JsonDocument doc;
  doc["type"] = type;
  const bool interrupt = !strcmp(type, "conversation.pause") || !strcmp(type, "response.cancel");
  if (interrupt) {
    // Close the local gate before sending. Face mode immediately starts
    // capturing again, but old response frames can still be queued on either
    // transport. Only Relay's ordered, matching acknowledgement releases them.
    pending_interrupt_id_ = ++last_interrupt_id_;
    if (pending_interrupt_id_ == 0) pending_interrupt_id_ = ++last_interrupt_id_;
    interrupt_started_ms_ = millis();
    doc["interrupt_id"] = pending_interrupt_id_;
  }
  String json;
  serializeJson(doc, json);
  const bool sent = send_text(json);
  if (sent && !strcmp(type, "audio.start")) context_reset_notice_pending_ = false;
  if (!sent && interrupt) {
    // Never reopen the gate on a failed send: the remote response may still
    // be streaming. A reconnect or a subsequent acknowledged pause recovers.
    if (state_handler_) state_handler_(AgentState::Error);
    send_notice("CONNECTION ERROR", "Could not interrupt Relay; reconnect to recover");
  }
  return sent;
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
      if (connected_) break;
      pending_interrupt_id_ = 0;
      connected_ = true;
      last_keepalive_ms_ = millis();
      Serial.println("Relay WebSocket connected");
      send_notice("CONNECTED", "Authenticating with Relay");
      send_hello();
      if (interrupt_recovery_id_ != 0) {
        interrupt_recovery_id_ = 0;
        context_reset_notice_pending_ = true;
        send_notice("CONVERSATION RESET", "Tap to start a new conversation");
      }
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
      if (connected_ && pending_interrupt_id_ == 0 && audio_handler_) {
        audio_handler_(payload, length);
      }
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
    case UsbFrameType::HostOpenAck: {
      // DeviceOpen retries can leave duplicate acks buffered. They neither
      // start a new session nor release an in-flight interruption fence.
      if (connected_) break;
      if (interrupt_recovery_id_ != 0) {
        // A queued pre-timeout open ack is not proof that the old connection
        // was closed. The bridge echoes this ID only after creating a fresh
        // WebSocket, including when DeviceOpen needs to be retried.
        JsonDocument ack;
        if (deserializeJson(ack, payload, length) || !ack["recovery_id"].is<uint32_t>() ||
            ack["recovery_id"].as<uint32_t>() != interrupt_recovery_id_) break;
      }
      pending_interrupt_id_ = 0;
      connected_ = true;
      last_keepalive_ms_ = millis();
      send_notice("USB CONNECTED", "Authenticating with Relay");
      send_hello();
      if (interrupt_recovery_id_ != 0) {
        interrupt_recovery_id_ = 0;
        context_reset_notice_pending_ = true;
        send_notice("CONVERSATION RESET", "Tap to start a new conversation");
      }
      break;
    }
    case UsbFrameType::HostClose:
      connected_ = false;
      if (pending_interrupt_id_ != 0) {
        // Close/open frames already in the serial queue can predate the tap.
        // Require a new correlated open even if this close beats the timeout.
        interrupt_recovery_id_ = pending_interrupt_id_;
        last_usb_open_ms_ = 0;
      }
      send_notice("USB DISCONNECTED", "PC bridge connection lost");
      if (state_handler_) state_handler_(AgentState::Disconnected);
      break;
    case UsbFrameType::HostText:
      handle_text(payload, length);
      break;
    case UsbFrameType::HostBinary:
      if (connected_ && pending_interrupt_id_ == 0 && audio_handler_) {
        audio_handler_(payload, length);
      }
      break;
    default:
      break;
  }
}

void RelayClient::handle_text(const uint8_t* payload, size_t length) {
  if (!connected_) return;
  // Search citations can exceed the old fixed 512-byte document. ArduinoJson
  // 7 grows this document as needed, avoiding a silent parse failure.
  JsonDocument doc;
  if (deserializeJson(doc, payload, length)) return;
  const char* message_type = doc["type"] | "";
  if (!strcmp(message_type, "conversation.paused")) {
    if (pending_interrupt_id_ != 0 && doc["interrupt_id"].is<uint32_t>() &&
        doc["interrupt_id"].as<uint32_t>() == pending_interrupt_id_) {
      pending_interrupt_id_ = 0;
    }
    return;
  }
  if (pending_interrupt_id_ != 0 && strcmp(message_type, "error")) {
    // Suppress response state, PCM (in both transport callbacks), completion,
    // emotion and notices. A stale Speaking or Ready must not disable the
    // microphone or change the Face UI while waiting for the fence. Session
    // notifications are not acknowledgements: they too may already be queued.
    if (strcmp(message_type, "state") || parse_state(doc["state"] | "") != AgentState::Error) {
      return;
    }
  }
  if (!strcmp(message_type, "state") && state_handler_) {
    state_handler_(parse_state(doc["state"] | ""));
  } else if (!strcmp(message_type, "session.ready")) {
    if (!context_reset_notice_pending_) send_notice("SESSION READY", "Relay connected to Foundry");
  } else if (!strcmp(message_type, "session.reconnected")) {
    context_reset_notice_pending_ = true;
    send_notice("CONVERSATION RESET", "Foundry reconnected; previous context lost");
    if (session_reconnected_handler_) session_reconnected_handler_();
  } else if (!strcmp(message_type, "hello.ack")) {
    if (!context_reset_notice_pending_) send_notice("DEVICE READY", "Tap the screen to speak");
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
