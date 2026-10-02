#pragma once

#include <Arduino.h>
#include <utility>

enum WStype_t {
  WStype_DISCONNECTED, WStype_CONNECTED, WStype_TEXT, WStype_BIN, WStype_ERROR
};

class WebSocketsClient {
 public:
  using Handler = std::function<void(WStype_t, uint8_t*, size_t)>;
  inline static WebSocketsClient* instance = nullptr;
  Handler handler;
  std::vector<std::string> sent_text;
  std::vector<uint8_t> sent_audio;
  bool send_ok = true;
  size_t disconnects = 0;

  WebSocketsClient() { instance = this; }
  void setExtraHeaders(const char*) {}
  void begin(const char*, uint16_t, const char*) {}
  void beginSSL(const char*, uint16_t, const char*) {}
  void setReconnectInterval(uint32_t) {}
  void enableHeartbeat(uint32_t, uint32_t, uint8_t) {}
  void onEvent(Handler value) { handler = std::move(value); }
  void disconnect() { ++disconnects; }
  void loop() {}
  bool sendTXT(String& text) {
    if (send_ok) sent_text.push_back(text);
    return send_ok;
  }
  bool sendBIN(const uint8_t* data, size_t length) {
    if (send_ok) sent_audio.insert(sent_audio.end(), data, data + length);
    return send_ok;
  }
  void emit(WStype_t type, const std::string& data = "") {
    if (handler) handler(type, reinterpret_cast<uint8_t*>(const_cast<char*>(data.data())), data.size());
  }
};
