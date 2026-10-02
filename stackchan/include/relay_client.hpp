#pragma once

#include <Arduino.h>
#include <WebSocketsClient.h>

#include <functional>

#include "protocol.hpp"
#include "usb_transport.hpp"

namespace stackchan {
enum class RelayTransport : uint8_t { None, Wifi, Usb };

class RelayClient {
 public:
  using AudioHandler = std::function<void(const uint8_t*, size_t)>;
  using StateHandler = std::function<void(AgentState)>;
  using NoticeHandler = std::function<void(const char*, const char*)>;
  using ResponseDoneHandler = std::function<void()>;
  using SessionReconnectedHandler = std::function<void()>;
  using EmotionHandler = std::function<void(const char*)>;

  void begin_wifi(const char* host, uint16_t port);
  void begin_usb();
  void end();
  void loop();
  bool connected() const { return connected_; }
  bool usb_host_available() const { return usb_.host_available(); }
  RelayTransport transport() const { return transport_; }
  void send_audio(const int16_t* samples, size_t sample_count);
  bool send_control(const char* type);
  void send_ui_mode(const char* mode);
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
  void handle_usb_frame(UsbFrameType type, const uint8_t* payload, size_t length);
  void handle_text(const uint8_t* payload, size_t length);
  void send_usb_open();
  void recover_interrupt_timeout();
  bool send_text(const String& text);
  void send_hello();
  void send_notice(const char* title, const char* detail);
  static AgentState parse_state(const char* value);

  WebSocketsClient ws_;
  UsbTransport usb_;
  RelayTransport transport_ = RelayTransport::None;
  bool connected_ = false;
  uint32_t last_keepalive_ms_ = 0;
  uint32_t last_usb_open_ms_ = 0;
  // IDs survive reconnects so a delayed acknowledgement cannot release a
  // newer interrupt. Zero means there is no outstanding output fence.
  uint32_t last_interrupt_id_ = 0;
  uint32_t pending_interrupt_id_ = 0;
  uint32_t interrupt_started_ms_ = 0;
  uint32_t interrupt_recovery_id_ = 0;
  bool context_reset_notice_pending_ = false;
  AudioHandler audio_handler_;
  StateHandler state_handler_;
  NoticeHandler notice_handler_;
  ResponseDoneHandler response_done_handler_;
  SessionReconnectedHandler session_reconnected_handler_;
  EmotionHandler emotion_handler_;
};
}  // namespace stackchan
