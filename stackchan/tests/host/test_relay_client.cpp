#include "relay_client.hpp"

#include <ArduinoJson.h>
#include <algorithm>
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
  std::string last_notice;
  bool usb = false;

  Harness() {
    Serial.reset();
    client.on_state([&](AgentState value) { state = value; ++states; });
    client.on_audio([&](const uint8_t*, size_t) { ++audio; });
    client.on_response_done([&]() { ++done; });
    client.on_emotion([&](const char*) { ++emotions; });
    client.on_notice([&](const char* title, const char*) { ++notices; last_notice = title; });
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
    require(client.send_control(type), "interrupt send reported failure");
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
  void tick(uint32_t ms) {
    test_clock_us += static_cast<uint64_t>(ms) * 1000;
    if (usb) receive_usb(UsbFrameType::HostProbe);
    client.loop();
  }
  void fresh_connection(uint32_t recovery_id) {
    if (usb) {
      receive_usb(UsbFrameType::HostOpenAck,
                  "{\"recovery_id\":" + std::to_string(recovery_id) + "}");
    } else {
      WebSocketsClient::instance->emit(WStype_CONNECTED);
    }
    require(client.connected(), "fresh handshake did not recover the connection");
    require(last_notice == "CONVERSATION RESET", "recovery did not disclose reset conversation context");
    text(R"({"type":"session.ready"})");
    text(R"({"type":"hello.ack"})");
    require(last_notice == "CONVERSATION RESET", "handshake notices erased the conversation reset disclosure");
    require(client.send_control("audio.start"), "fresh connection could not start listening");
    states = audio = done = emotions = 0;
    require_new_response();
  }
  std::vector<UsbFrameType> outgoing_usb_types() {
    std::vector<UsbFrameType> types;
    UsbTransport decoder;
    decoder.on_frame([&](UsbFrameType type, const uint8_t* data, size_t size) {
      types.push_back(type);
      if (type == UsbFrameType::DeviceOpen) {
        JsonDocument doc;
        require(!deserializeJson(doc, data, size), "invalid recovery DeviceOpen JSON");
        require(doc["recovery_id"].is<uint32_t>() && doc["recovery_id"].as<uint32_t>() != 0,
                "recovery DeviceOpen omitted its correlation ID");
      }
    });
    Serial.input.insert(Serial.input.end(), Serial.output.begin(), Serial.output.end());
    while (Serial.available()) decoder.loop();
    return types;
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
      test_clock_us += 4'000'000;
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
      require(!h.client.send_control("conversation.pause"), "failed interrupt incorrectly reported success");
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
      require(!h.client.connected(), "queued bare USB open ack released the interruption fence");
      h.states = h.audio = h.done = h.emotions = 0;
      h.state = AgentState::Listening;
      h.stale_response();
      h.require_suppressed();
      h.fresh_connection(usb_id);
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
    } else if (name == "lost_ack" || name == "timeout_wrap") {
      if (name == "timeout_wrap") test_clock_us = (UINT32_MAX - 2000ULL) * 1000;
      const uint32_t id = h.interrupt();
      const size_t disconnects = WebSocketsClient::instance->disconnects;
      h.tick(4999);
      require(h.client.connected(), "fence forced a reconnect before its deadline");
      h.stale_response();
      h.require_suppressed();
      h.tick(1);
      require(!h.client.connected(), "lost pause acknowledgement left connection fenced forever");
      if (use_usb) {
        const auto types = h.outgoing_usb_types();
        require(std::find(types.begin(), types.end(), UsbFrameType::DeviceClose) != types.end(),
                "USB timeout did not request a real connection close");
        require(std::find(types.begin(), types.end(), UsbFrameType::DeviceOpen) != types.end(),
                "USB timeout did not request a fresh connection open");
        h.receive_usb(UsbFrameType::HostOpenAck);  // old queued handshake
        h.receive_usb(UsbFrameType::HostOpenAck, "{\"recovery_id\":" + std::to_string(id + 1) + "}");
        require(!h.client.connected(), "old USB handshake reopened a timed-out connection");
      } else {
        require(WebSocketsClient::instance->disconnects == disconnects + 1,
                "WiFi timeout did not disconnect the actual WebSocket");
      }
      h.states = 0;
      h.state = AgentState::Listening;
      h.ack(id);  // a late ack cannot undo a transport reconnect in progress
      h.stale_response();
      h.require_suppressed();
      h.tick(20'000);
      h.require_suppressed();
      if (!use_usb) require(WebSocketsClient::instance->disconnects == disconnects + 1,
                           "pending recovery repeatedly forced WiFi disconnects");
      h.fresh_connection(id);
      require(h.interrupt() > id, "timeout recovery reused an interrupt ID");
    } else if (name == "timeout_repeated") {
      const uint32_t first = h.interrupt();
      h.tick(4000);
      const uint32_t latest = h.interrupt();
      h.tick(4000);
      h.ack(first);
      h.ack(latest + 1);
      h.text(R"({"type":"conversation.paused","interrupt_id":"bad"})");
      h.tick(999);
      require(h.client.connected(), "new user interrupt did not receive its own ack deadline");
      h.tick(1);
      require(!h.client.connected(), "stale/malformed ack extended the current fence deadline");
      h.fresh_connection(latest);
    } else if (name == "acked_deadline") {
      const uint32_t id = h.interrupt();
      h.tick(4999);
      h.ack(id);
      h.tick(20'000);
      require(h.client.connected(), "acknowledged fence later forced a reconnect");
      h.require_new_response();
    } else if (name == "timeout_switch") {
      h.interrupt();
      h.tick(4000);
      h.client.end();
      h.begin(!use_usb);
      h.tick(20'000);
      require(h.client.connected(), "old fence deadline disconnected a newly selected transport");
      h.require_new_response();
    } else if (name == "context_reset") {
      h.text(R"({"type":"session.reconnected"})");
      require(h.reconnects == 1 && h.last_notice == "CONVERSATION RESET",
              "upstream reconnect did not disclose conversation reset");
      h.text(R"({"type":"session.ready"})");
      h.text(R"({"type":"hello.ack"})");
      require(h.last_notice == "CONVERSATION RESET", "session ready erased the context reset notice");
      require(h.client.send_control("audio.start"), "new listening request failed");
      h.text(R"({"type":"hello.ack"})");
      require(h.last_notice == "DEVICE READY", "context reset disclosure did not clear for new conversation");
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
