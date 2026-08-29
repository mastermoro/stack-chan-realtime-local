#pragma once

#include <Arduino.h>

#include <functional>
#include <vector>

namespace stackchan {
enum class UsbFrameType : uint8_t {
  HostProbe = 0x01,
  DeviceStatus = 0x02,
  DeviceOpen = 0x03,
  HostOpenAck = 0x04,
  DeviceClose = 0x05,
  HostClose = 0x06,
  DeviceText = 0x10,
  DeviceBinary = 0x11,
  HostText = 0x20,
  HostBinary = 0x21,
  DeviceLog = 0x30,
  Error = 0x7f,
};

class UsbTransport {
 public:
  using FrameHandler = std::function<void(UsbFrameType, const uint8_t*, size_t)>;

  void begin();
  void loop();
  bool send(UsbFrameType type, const uint8_t* payload = nullptr, size_t length = 0);
  bool host_available() const;
  void on_frame(FrameHandler handler) { frame_handler_ = std::move(handler); }
  uint32_t invalid_frames() const { return invalid_frames_; }

 private:
  void consume_packet();
  bool write_all(const uint8_t* data, size_t length);

  std::vector<uint8_t> encoded_input_;
  std::vector<uint8_t> decoded_input_;
  std::vector<uint8_t> transmit_raw_;
  std::vector<uint8_t> transmit_encoded_;
  FrameHandler frame_handler_;
  uint16_t transmit_sequence_ = 0;
  uint32_t last_host_probe_ms_ = 0;
  uint32_t invalid_frames_ = 0;
};
}  // namespace stackchan
