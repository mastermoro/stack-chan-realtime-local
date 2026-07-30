#pragma once

#include <Arduino.h>
#include <WebSocketsClient.h>

#include <functional>

#include "protocol.hpp"

namespace stackchan {
class RelayClient {
 public:
  using AudioHandler = std::function<void(const uint8_t*, size_t)>;
  using StateHandler = std::function<void(AgentState)>;
  using NoticeHandler = std::function<void(const char*, const char*)>;
  using ResponseDoneHandler = std::function<void()>;
  using SessionReconnectedHandler = std::function<void()>;
  using EmotionHandler = std::function<void(const char*)>;

  void begin();
  void loop();
  bool connected() const { return connected_; }
  void send_audio(const int16_t* samples, size_t sample_count);
  void send_control(const char* type);
  void on_audio(AudioHandler handler) { audio_handler_ = std::move(handler); }
  void on_state(StateHandler handler) { state_handler_ = std::move(handler); }
  void on_notice(NoticeHandler handler) { notice_handler_ = std::move(handler); }
  void on_response_done(ResponseDoneHandler handler) { response_done_handler_ = std::move(handler); }
  void on_session_reconnected(SessionReconnectedHandler handler) {
    session_reconnected_handler_ = std::move(handler);
  }
  void on_emotion(EmotionHandler handler) { emotion_handler_ = std::move(handler); }

 private:
  void handle_event(WStype_t type, uint8_t* payload, size_t length);
  void send_hello();
  void send_notice(const char* title, const char* detail);
  static AgentState parse_state(const char* value);

  WebSocketsClient ws_;
  bool connected_ = false;
  uint32_t last_keepalive_ms_ = 0;
  AudioHandler audio_handler_;
  StateHandler state_handler_;
  NoticeHandler notice_handler_;
  ResponseDoneHandler response_done_handler_;
  SessionReconnectedHandler session_reconnected_handler_;
  EmotionHandler emotion_handler_;
};
}  // namespace stackchan
