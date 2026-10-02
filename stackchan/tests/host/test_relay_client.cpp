#include "relay_client.hpp"

#include <ArduinoJson.h>
#include <cstdlib>
#include <iostream>
#include <stdexcept>
#include <string>

using namespace stackchan;
void require(bool value, const char* message) {
  if (!value) throw std::runtime_error(message);
}

struct Harness {
  RelayClient client;
  AgentState state = AgentState::Listening;
  size_t states = 0;
  size_t audio = 0;
  size_t done = 0;
  size_t emotions = 0;
  size_t notices = 0;
  size_t reconnects = 0;
  bool usb = false;

  Harness() {
    Serial.reset();
    client.on_state([&](AgentState value) { state = value; ++states; });
    client.on_audio([&](const uint8_t*, size_t) { ++audio; });
    client.on_response_done([&]() { ++done; });
    client.on_emotion([&](const char*) { ++emotions; });
    client.on_notice([&](const char*, const char*) { ++notices; });
    client.on_session_reconnected([&]() { ++reconnects; });
  }
  void begin(bool use_usb = false) {
    usb = use_usb;
    if (usb) {
      client.begin_usb();
      receive_usb(UsbFrameType::HostOpenAck);
    } else {
      client.begin_wifi("example.test", 80);
      WebSocketsClient::instance->emit(WStype_CONNECTED);
    }
    require(client.connected(), "test transport did not connect");
    states = audio = done = emotions = notices = reconnects = 0;
    state = AgentState::Listening;
  }
  void receive_usb(UsbFrameType type, const std::string& payload = "") {
    UsbTransport sender;
    const auto previous_output = Serial.output;
    Serial.output.clear();
    require(sender.send(type, reinterpret_cast<const uint8_t*>(payload.data()), payload.size()), "could not encode test frame");
    const auto packet = Serial.output;
    Serial.output = previous_output;
    Serial.input.insert(Serial.input.end(), packet.begin(), packet.end());
    while (Serial.available()) client.loop();
  }
  void text(const std::string& payload) {
    if (usb) receive_usb(UsbFrameType::HostText, payload);
    else WebSocketsClient::instance->emit(WStype_TEXT, payload);
  }
  void pcm() {
    if (usb) receive_usb(UsbFrameType::HostBinary, "\1\2\3\4");
    else WebSocketsClient::instance->emit(WStype_BIN, "\1\2\3\4");
  }
  void stale_response() {
    text(R"({"type":"state","state":"speaking"})");
    pcm();
    text(R"({"type":"response.done"})");
    text(R"({"type":"emotion","emotion":"happy"})");
    text(R"({"type":"state","state":"ready"})");
  }
  void require_suppressed() {
    require(states == 0 && state == AgentState::Listening, "stale response state clobbered local listening");
    require(audio == 0, "stale PCM reached playback callback");
    require(done == 0, "stale response.done reached callback");
    require(emotions == 0, "stale emotion reached callback");
  }
  void require_new_response() {
    stale_response();
    require(states == 2 && audio == 1 && done == 1 && emotions == 1, "fresh response did not resume after fence");
  }
  uint32_t interrupt(const char* type = "conversation.pause") {
    Serial.output.clear();
    WebSocketsClient::instance->sent_text.clear();
    client.send_control(type);
    std::string sent;
    if (usb) {
      UsbTransport decoder;
      decoder.on_frame([&](UsbFrameType frame_type, const uint8_t* data, size_t size) {
        if (frame_type == UsbFrameType::DeviceText) sent.assign(reinterpret_cast<const char*>(data), size);
      });
      const auto outgoing = Serial.output;
      Serial.input.insert(Serial.input.end(), outgoing.begin(), outgoing.end());
      while (Serial.available()) decoder.loop();
    } else {
      require(!WebSocketsClient::instance->sent_text.empty(), "interrupt control was not sent");
      sent = WebSocketsClient::instance->sent_text.back();
    }
    JsonDocument doc;
    require(!deserializeJson(doc, sent), "outgoing control is invalid JSON");
    require(doc["interrupt_id"].is<uint32_t>() && doc["interrupt_id"].as<uint32_t>() != 0,
            "interrupt control lacks a positive numeric interrupt_id");
    require(std::string(doc["type"].as<const char*>()) == type, "interrupt changed control type");
    return doc["interrupt_id"].as<uint32_t>();
  }
  void ack(uint32_t id) {
    text("{\"type\":\"conversation.paused\",\"interrupt_id\":" + std::to_string(id) + "}");
  }
};

int main(int argc, char** argv) {
  try {
    require(argc == 2, "expected test name");
    Harness h;
    const std::string requested_name = argv[1];
    const bool use_usb = requested_name.rfind("usb_", 0) == 0;
    const std::string name = use_usb ? requested_name.substr(4) : requested_name;
    h.begin(use_usb);
    if (name == "wifi_fence" || name == "fence") {
      // Model the existing Face branch: stop locally, pause, then immediately
      // enter Listening while already-received response frames are queued.
      h.client.send_control("conversation.pause");
      h.stale_response();
      h.require_suppressed();
      const uint32_t id = h.interrupt();
      test_clock_us += 60'000'000;
      h.stale_response();
      h.require_suppressed();
      h.ack(id);
      h.require_new_response();
    } else if (name == "exact_ack") {
      const uint32_t id = h.interrupt();
      h.text(R"({"type":"conversation.paused"})");
      h.text("{\"type\":\"conversation.paused\",\"interrupt_id\":\"" + std::to_string(id) + "\"}");
      h.text(R"({"type":"conversation.paused","interrupt_id":true})");
      h.text(R"({"type":"conversation.paused","interrupt_id":-1})");
      h.text(R"({"type":"conversation.paused","interrupt_id":1.0})");
      h.ack(id + 1);
      h.stale_response();
      h.require_suppressed();
      h.ack(id);
      h.require_new_response();
    } else if (name == "repeated_interrupts") {
      const uint32_t first = h.interrupt();
      const uint32_t second = h.interrupt("response.cancel");
      require(second > first, "repeated interrupts reused an ID");
      h.ack(first);
      h.stale_response();
      h.require_suppressed();
      h.ack(second);
      h.require_new_response();
      h.states = h.audio = h.done = h.emotions = 0;
      h.state = AgentState::Listening;
      const uint32_t third = h.interrupt();
      h.ack(second);
      h.stale_response();
      h.require_suppressed();
      h.ack(third);
      h.require_new_response();
    } else if (name == "failed_send") {
      WebSocketsClient::instance->send_ok = false;
      Serial.fail_write = true;
      h.client.send_control("conversation.pause");
      require(h.notices > 0, "failed interrupt send was silently ignored");
      Serial.fail_write = false;
      // A genuine connection error may be shown, but no response may leak.
      h.states = 0;
      h.state = AgentState::Listening;
      h.stale_response();
      h.require_suppressed();
    } else if (name == "reconnect") {
      const uint32_t first = h.interrupt();
      h.text(R"({"type":"session.ready"})");
      h.text(R"({"type":"hello.ack"})");
      h.text(R"({"type":"session.reconnected"})");
      require(h.reconnects == 0, "queued session notification changed listening resume state");
      h.stale_response();
      h.require_suppressed();
      WebSocketsClient::instance->emit(WStype_DISCONNECTED);
      require(h.state == AgentState::Disconnected, "fence hid a real disconnect");
      WebSocketsClient::instance->emit(WStype_CONNECTED);
      h.states = h.audio = h.done = h.emotions = 0;
      h.require_new_response();
      require(h.interrupt() > first, "reconnect reused an interrupt ID");
      h.client.end();
      h.begin(true);
      const uint32_t usb_id = h.interrupt();
      h.notices = 0;
      h.receive_usb(UsbFrameType::HostOpenAck);  // duplicate, not a new connection
      require(h.notices == 0, "duplicate USB open ack overwrote the listening UI");
      h.stale_response();
      h.require_suppressed();
      h.receive_usb(UsbFrameType::HostClose);
      require(h.state == AgentState::Disconnected, "USB fence hid a real disconnect");
      h.receive_usb(UsbFrameType::HostOpenAck);
      h.states = h.audio = h.done = h.emotions = 0;
      h.require_new_response();
      require(h.interrupt() > usb_id, "USB reconnect reused an interrupt ID");
    } else if (name == "notices") {
      h.interrupt();
      h.text(R"({"type":"notice","title":"SPEAKING","detail":"old turn"})");
      h.text(R"({"type":"sources","sources":[{"title":"old search"}]})");
      h.text(R"({"type":"session.ready"})");
      h.text(R"({"type":"hello.ack"})");
      h.stale_response();
      require(h.notices == 0, "stale response notice overwrote listening UI");
      h.text(R"({"type":"error","code":"UPSTREAM_RECONNECTING","message":"retrying"})");
      require(h.notices == 1, "fence hid reconnect error");
      h.text(R"({"type":"error","code":"RELAY ERROR","message":"failed"})");
      require(h.notices == 2 && h.state == AgentState::Error, "fence hid a real error");
    } else if (name == "audio_while_fenced") {
      const uint32_t id = h.interrupt();
      h.client.send_control("audio.start");
      const int16_t samples[] = {4, 5};
      h.client.send_audio(samples, 2);
      require(WebSocketsClient::instance->sent_audio.size() == sizeof(samples), "fence blocked immediate microphone capture");
      require(WebSocketsClient::instance->sent_text.back().find("audio.start") != std::string::npos, "fence blocked immediate listening control");
      h.stale_response();
      h.require_suppressed();
      h.ack(id);
      h.require_new_response();
    } else {
      throw std::runtime_error("unknown test");
    }
    std::cout << "PASS relay/" << requested_name << '\n';
    return EXIT_SUCCESS;
  } catch (const std::exception& error) {
    std::cerr << "FAIL relay/" << (argc > 1 ? argv[1] : "?") << ": " << error.what() << '\n';
    return EXIT_FAILURE;
  }
}
