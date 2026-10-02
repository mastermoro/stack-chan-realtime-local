#include "usb_transport.hpp"

#include <cstdlib>
#include <iostream>
#include <stdexcept>
#include <string>

using namespace stackchan;
void require(bool value, const char* message) {
  if (!value) throw std::runtime_error(message);
}

std::vector<uint8_t> frame(const std::vector<uint8_t>& payload) {
  UsbTransport sender;
  Serial.output.clear();
  require(sender.send(UsbFrameType::HostBinary, payload.data(), payload.size()), "frame encoding failed");
  return Serial.output;
}

int main(int argc, char** argv) {
  try {
    require(argc == 2, "expected test name");
    Serial.reset();
    UsbTransport receiver;
    receiver.begin();
    std::vector<std::vector<uint8_t>> received;
    receiver.on_frame([&](UsbFrameType type, const uint8_t* data, size_t size) {
      require(type == UsbFrameType::HostBinary, "wrong frame type");
      received.emplace_back(data, data + size);
    });
    const std::string name = argv[1];
    if (name == "sustained_stream") {
      const std::vector<uint8_t> payload(960, 42);
      const auto packet = frame(payload);
      Serial.input.insert(Serial.input.end(), packet.begin(), packet.end());
      size_t supplied = 1;
      Serial.after_read = [&]() {
        if (Serial.input.empty() && supplied++ < 100) {
          Serial.input.insert(Serial.input.end(), packet.begin(), packet.end());
        }
      };
      receiver.loop();
      require(Serial.reads <= 2048, "continuous receive monopolized main loop instead of yielding by byte budget");
      require(!received.empty(), "receive budget made no forward progress");
      require(Serial.available() > 0, "test producer should still be streaming when loop yields");
      Serial.after_read = nullptr;
      while (Serial.available()) receiver.loop();
      require(receiver.invalid_frames() == 0, "yield damaged a partial frame");
      for (const auto& actual : received) require(actual == payload, "payload changed across yields");
    } else if (name == "time_budget") {
      const auto packet = frame(std::vector<uint8_t>(960, 7));
      Serial.input.insert(Serial.input.end(), packet.begin(), packet.end());
      Serial.read_duration_us = 100;
      const uint64_t start = test_clock_us;
      receiver.loop();
      require(test_clock_us - start <= 2100, "receive failed to yield after elapsed-time budget");
      require(Serial.available() > 0, "time budget failed to retain unfinished frame");
      while (Serial.available()) receiver.loop();
      require(received.size() == 1 && received[0] == std::vector<uint8_t>(960, 7), "partial frame failed after time slice");
    } else if (name == "packet_budget") {
      const auto packet = frame({1, 2});
      for (size_t i = 0; i < 40; ++i) Serial.input.insert(Serial.input.end(), packet.begin(), packet.end());
      receiver.loop();
      require(received.size() <= 4, "receive monopolized main loop with packet callbacks");
      require(!received.empty(), "packet budget made no forward progress");
      while (Serial.available()) receiver.loop();
      require(received.size() == 40, "packet budget lost frames");
    } else if (name == "maximum_frame") {
      std::vector<uint8_t> payload(8192);
      for (size_t i = 0; i < payload.size(); ++i) payload[i] = static_cast<uint8_t>(i);
      const auto packet = frame(payload);
      Serial.input.insert(Serial.input.end(), packet.begin(), packet.end());
      receiver.loop();
      require(received.empty() && Serial.available() > 0, "maximum packet was not split across byte budgets");
      while (Serial.available()) receiver.loop();
      require(received.size() == 1 && received[0] == payload, "maximum frame failed across receive yields");
    } else if (name == "micros_wrap") {
      const auto packet = frame(std::vector<uint8_t>(960, 7));
      Serial.input.insert(Serial.input.end(), packet.begin(), packet.end());
      test_clock_us = UINT32_MAX - 500ULL;
      Serial.read_duration_us = 100;
      const uint64_t start = test_clock_us;
      receiver.loop();
      require(test_clock_us - start <= 2100, "receive time budget failed across micros wrap");
    } else {
      throw std::runtime_error("unknown test");
    }
    std::cout << "PASS usb/" << name << '\n';
    return EXIT_SUCCESS;
  } catch (const std::exception& error) {
    std::cerr << "FAIL usb/" << (argc > 1 ? argv[1] : "?") << ": " << error.what() << '\n';
    return EXIT_FAILURE;
  }
}
